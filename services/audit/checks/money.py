"""Проверки раздела «Деньги»: дубли платежей."""

from collections import Counter, defaultdict
from datetime import datetime, timedelta

from services.audit.context import AuditContext, format_moment, is_marketplace, parse_moment
from services.audit.specs import CheckSpec, RawFinding, Section, Severity

_DUPLICATE_WINDOW_DAYS = 14

# статусы заказа, при которых оплата не аванс, а деньги к возврату
_CANCELLED_ORDER_MARKERS = ('отмен', 'возврат')


def _is_cancelled_order(status: str) -> bool:
    low = (status or '').lower()
    return any(m in low for m in _CANCELLED_ORDER_MARKERS)


def _within_window(sorted_docs: list) -> list:
    """Оставить самую большую группу платежей, попарно ближе окна друг к другу."""
    best: list = []
    for i, (_, anchor) in enumerate(sorted_docs):
        try:
            anchor_dt = parse_moment(anchor['moment'])
        except (KeyError, ValueError):
            continue
        cluster = [sorted_docs[i]]
        for entity, d in sorted_docs[i + 1:]:
            try:
                if parse_moment(d['moment']) - anchor_dt <= timedelta(days=_DUPLICATE_WINDOW_DAYS):
                    cluster.append((entity, d))
            except (KeyError, ValueError):
                continue
        if len(cluster) > len(best):
            best = cluster
    return best


class PaymentDuplicateCheck(CheckSpec):
    """Два исходящих платежа одному контрагенту на одну сумму в близкие даты.

    Живой кейс: №00082 и №00086 оба проведены на 21 070 ₽ по заказу 00049.
    LLM решает: дубль или законная повторная оплата одинаковых счетов."""

    id = 'payment_duplicate'
    section = Section.MONEY
    title = 'Возможный дубль платежа'
    default_severity = Severity.CRITICAL
    supports_incremental = False   # дубль ищется по всему окну, не по инкременту

    async def detect(self, ctx: AuditContext, since: datetime | None) -> list[RawFinding]:
        payments = []
        for entity in ('paymentout', 'cashout'):
            docs = await ctx.cached_list(
                f'{entity}_3m', entity,
                filters=f'moment>={ctx.scan_since_moment}',
                expand='agent', order='moment,desc',
            )
            for d in docs:
                if d.get('applicable') is False:
                    continue
                payments.append((entity, d))

        groups: dict[tuple, list] = defaultdict(list)
        for entity, d in payments:
            agent_href = ((d.get('agent') or {}).get('meta') or {}).get('href', '')
            groups[(agent_href, d.get('sum', 0))].append((entity, d))

        out = []
        for (agent_href, total), docs in groups.items():
            if len(docs) < 2 or total == 0:
                continue
            docs.sort(key=lambda x: x[1].get('moment', ''))
            # Регулярные платежи (аренда, комиссии банка) идут с месячным шагом —
            # дублем считаем только платежи в пределах _DUPLICATE_WINDOW_DAYS
            docs = _within_window(docs)
            if len(docs) < 2:
                continue
            details = [{
                'entity_type': e,
                'entity_id': d.get('id'),
                'name': d.get('name'),
                'moment': (d.get('moment') or '')[:16],
                'sum_kopecks': d.get('sum', 0),
                'purpose': (d.get('paymentPurpose') or '')[:150],
                'description': (d.get('description') or '')[:150],
            } for e, d in docs]
            first_entity, first = docs[0]
            agent_name = ((first.get('agent') or {}).get('name')
                          if isinstance(first.get('agent'), dict) else '')
            out.append(RawFinding(
                entity_type=first_entity,
                entity_id=first['id'],
                entity_href=first['meta']['href'],
                entity_name=(f'{len(docs)} платежа по {total / 100:,.2f} ₽ — '
                             f'{agent_name or "контрагент не указан"}').replace(',', ' '),
                severity=self.default_severity,
                payload={'agent': agent_name, 'sum_kopecks': total, 'payments': details},
                fingerprint_salt='|'.join(d['name'] for _, d in docs),
            ))
        return out

    def explain(self, payload: dict) -> str:
        n = len(payload.get('payments', []))
        return (f'{n} проведённых платежа одному контрагенту на одинаковую сумму — '
                f'возможен дубль, деньги могли уйти дважды.')


class DuplicateCounterpartyCheck(CheckSpec):
    """Две карточки одного контрагента: одинаковый ИНН или одинаковое имя.

    Документы расходятся по двум карточкам, и баланс контрагента перестаёт
    сходиться в обе стороны. Живой кейс: Озон завели второй раз, и за день
    на новую карточку ушло шесть заказов на 61 600 ₽, пока 190 документов
    висели на основной."""

    id = 'duplicate_counterparty'
    section = Section.MONEY
    title = 'Дубль контрагента в справочнике'
    default_severity = Severity.IMPORTANT
    supports_incremental = False

    async def detect(self, ctx: AuditContext, since: datetime | None) -> list[RawFinding]:
        agents = await ctx.cached_list('counterparty_all', 'counterparty', max_rows=2000)
        active = [a for a in agents if not a.get('archived')]

        groups: dict[tuple, list] = defaultdict(list)
        for a in active:
            inn = (a.get('inn') or '').strip()
            key = ('инн', inn) if inn else ('имя', (a.get('name') or '').strip().lower())
            if key[1]:
                groups[key].append(a)

        # сколько документов на каждой карточке — показывает, какая основная
        usage: Counter = Counter()
        for entity in ('supply', 'demand', 'customerorder', 'purchaseorder',
                       'paymentin', 'paymentout', 'cashin', 'cashout'):
            for d in await ctx.cached_list(f'{entity}_balance_full', entity,
                                           expand='agent', order='moment,desc', max_rows=2000):
                href = ((d.get('agent') or {}).get('meta') or {}).get('href', '')
                if href:
                    usage[href.split('/')[-1].split('?')[0]] += 1

        out = []
        for (kind, value), items in groups.items():
            if len(items) < 2:
                continue
            cards = sorted(({
                'name': a.get('name'),
                'inn': (a.get('inn') or '').strip() or None,
                'created': (a.get('created') or '')[:10],
                'documents': usage.get(a['id'], 0),
            } for a in items), key=lambda c: -c['documents'])
            main = items[0]
            out.append(RawFinding(
                entity_type='counterparty',
                entity_id=main['id'],
                entity_href=main['meta']['href'],
                entity_name=f'{main.get("name")} — {len(items)} карточки',
                severity=self.default_severity,
                payload={
                    'match_by': 'ИНН' if kind == 'инн' else 'название',
                    'value': value,
                    'cards': cards,
                    'note': ('Документы одного контрагента разошлись по разным карточкам: '
                             'баланс, взаиморасчёты и история покупок считаются по каждой '
                             'отдельно. Исправление: перенести документы на карточку, где '
                             'их больше, и убрать вторую в архив.'),
                },
                fingerprint_salt='|'.join(sorted(a['id'] for a in items)),
                ui_link=(main.get('meta') or {}).get('uuidHref', ''),
            ))
        return out

    def explain(self, payload: dict) -> str:
        cards = payload.get('cards', [])
        counts = ' и '.join(f'{c["documents"]} док.' for c in cards)
        return (f'Контрагент заведён дважды (совпадает {payload.get("match_by")}): '
                f'{counts}. Баланс и взаиморасчёты разъезжаются.')


class CounterpartyBalanceCheck(CheckSpec):
    """Расчётный баланс контрагента по документам за окно сканирования.

    Отчёт взаиморасчётов МойСклад недоступен на текущем тарифе (403, код 1043 —
    «тариф не позволяет работать с CRM»), поэтому считаем сами:
    поставщики — приёмки vs исходящие платежи, покупатели — отгрузки vs входящие.
    Считаем по ВСЕЙ истории: окно сканирования разрезает пары «платёж-приёмка»
    на своём краю и даёт фантомные долги (живой кейс: платёж 04.04
    выпал из окна, приёмка 09.04 попала → ложные «не оплачено 165 тыс»).
    LLM отличает норму (отсрочка маркетплейса, бесплатный поставщик, свежий заказ)
    от проблемы (переплата, дубль, забытая оплата)."""

    id = 'counterparty_balance'
    section = Section.MONEY
    title = 'Баланс контрагента не сходится'
    default_severity = Severity.IMPORTANT
    supports_incremental = False   # баланс — агрегат, не инкремент

    # хвосты от округлений и копеечные недоплаты (44 ₽ при обороте 22 тыс.)
    # только зашумляют: значимым считаем расхождение от 100 ₽
    _BALANCE_THRESHOLD_KOPECKS = 10000

    async def detect(self, ctx: AuditContext, since: datetime | None) -> list[RawFinding]:
        async def _agent_sums(entity: str, key: str, acc: dict):
            docs = await ctx.cached_list(
                f'{entity}_balance_full', entity,
                expand='agent', order='moment,desc', max_rows=2000,
            )
            for d in docs:
                if d.get('applicable') is False:
                    continue
                agent = d.get('agent') if isinstance(d.get('agent'), dict) else None
                href = ((agent or {}).get('meta') or {}).get('href', '')
                if not href:
                    continue
                if d.get('noClosingDocs'):
                    # «Без закрывающих документов» — МойСклад исключает такой платёж
                    # из взаиморасчётов (стандарт для доставки/услуг), зеркалим
                    continue
                slot = acc.setdefault(href, {
                    'agent': (agent or {}).get('name', '?'),
                    'supplies': 0, 'paid_out': 0, 'demands': 0, 'paid_in': 0,
                    'docs': [],
                })
                slot[key] += d.get('sum', 0)
                slot['docs'].append(f'{entity} №{d.get("name")} {d.get("moment", "")[:10]} '
                                    f'{d.get("sum", 0) / 100:.2f}')

        acc: dict[str, dict] = {}
        await _agent_sums('supply', 'supplies', acc)
        await _agent_sums('paymentout', 'paid_out', acc)
        await _agent_sums('cashout', 'paid_out', acc)
        await _agent_sums('demand', 'demands', acc)
        await _agent_sums('paymentin', 'paid_in', acc)
        await _agent_sums('cashin', 'paid_in', acc)

        # Комиссионеры: отгрузка им — передача товара на реализацию, а не продажа.
        # Долг возникает по мере продаж, которые фиксирует отчёт комиссионера
        # (живой кейс: Каприолю отгружено 327 763 ₽, продано 78 680 ₽ — остальное
        # лежит на его полке и нашим долгом не является).
        commission_agents: set[str] = set()
        for c in await ctx.cached_list('contract_all', 'contract', expand='agent', max_rows=500):
            if (c.get('contractType') or '') != 'Commission':
                continue
            href = ((c.get('agent') or {}).get('meta') or {}).get('href', '')
            if href:
                commission_agents.add(href.split('?')[0])
        sold_by_commission: dict[str, int] = {}
        for r in await ctx.cached_list('commissionreportin_all', 'commissionreportin',
                                       expand='agent', order='moment,desc', max_rows=1000):
            if r.get('applicable') is False:
                continue
            href = ((r.get('agent') or {}).get('meta') or {}).get('href', '').split('?')[0]
            if href:
                sold_by_commission[href] = sold_by_commission.get(href, 0) + r.get('sum', 0)

        # Недопоставленные заказы поставщикам: «переплата», покрытая ожидаемой
        # поставкой, — это аванс, а не проблема (кейс Тара.ру: заказ «Заказано»,
        # оплачен, тара ещё едет)
        pending_orders: dict[str, list] = {}
        orders = await ctx.cached_list(
            'purchaseorder_balance_full', 'purchaseorder',
            expand='agent', order='moment,desc', max_rows=2000,
        )
        for o in orders:
            if o.get('applicable') is False:
                continue
            remaining = o.get('sum', 0) - o.get('shippedSum', 0)
            if remaining <= 0:
                continue
            agent = o.get('agent') if isinstance(o.get('agent'), dict) else None
            href = ((agent or {}).get('meta') or {}).get('href', '')
            if href:
                pending_orders.setdefault(href, []).append({
                    'order': f'Заказ поставщику №{o.get("name")} от {(o.get("moment") or "")[:10]}',
                    'awaiting_kopecks': remaining,
                })

        # Заказы покупателей в работе. Недоплата — возможный долг: при статусе
        # ожидания оплаты / договорённости о постоплате в комментарии это норма
        # (кейс Александровой: «Ждем оплату», «оплата будет после 10го»), кодом
        # не подавляем — судит LLM. А оплаченная и ещё не отгруженная часть —
        # аванс под заказ, и это норма симметрично авансу поставщику (кейс
        # Медведевой: заказ 00221 оплачен 23 250 ₽, статус «Можно собирать»,
        # отгрузки пока нет — деньги на балансе покрыты заказом).
        # Отменённый заказ авансом не считаем: такие деньги подлежат возврату.
        pending_customer: dict[str, list] = {}
        prepaid_unshipped: dict[str, int] = {}
        customer_orders = await ctx.cached_list(
            'customerorder_balance_full', 'customerorder',
            expand='agent,state', order='moment,desc', max_rows=2000,
        )
        for o in customer_orders:
            if o.get('applicable') is False:
                continue
            order_sum = o.get('sum', 0)
            payed, shipped = o.get('payedSum') or 0, o.get('shippedSum') or 0
            unpaid = order_sum - payed
            prepaid = min(payed, order_sum) - shipped   # оплачено, но не отгружено
            if unpaid <= 0 and prepaid <= 0:
                continue
            agent = o.get('agent') if isinstance(o.get('agent'), dict) else None
            href = ((agent or {}).get('meta') or {}).get('href', '')
            if not href:
                continue
            status = ((o.get('state') or {}).get('name')) or ''
            if prepaid > 0 and not _is_cancelled_order(status):
                prepaid_unshipped[href] = prepaid_unshipped.get(href, 0) + prepaid
            pending_customer.setdefault(href, []).append({
                'order': f'Заказ покупателя №{o.get("name")} от {(o.get("moment") or "")[:10]}',
                'awaiting_payment_rub': round(max(unpaid, 0) / 100, 2),
                'paid_awaiting_shipment_rub': round(max(prepaid, 0) / 100, 2),
                'status': status or None,
                'comment': (o.get('description') or '')[:200],
            })

        out = []
        for href, s in acc.items():
            if not s['supplies'] and not s['demands']:
                # только платежи, товарных документов нет — операционные расходы
                # (банк, подписки, доставка); взаиморасчёт по товарам не применим
                continue
            supplier_balance = s['paid_out'] - s['supplies']   # >0 — переплатили поставщику
            on_commission = href in commission_agents or href in sold_by_commission
            sold = sold_by_commission.get(href, 0)
            # у комиссионера продажей считается отчёт, а не отгрузка
            customer_base = sold if on_commission else s['demands']
            customer_balance = customer_base - s['paid_in']     # >0 — нам должны
            on_shelf = s['demands'] - sold if on_commission else 0
            awaiting = sum(p['awaiting_kopecks'] for p in pending_orders.get(href, []))
            problems = []
            if s['supplies'] or s['paid_out']:
                if (supplier_balance > self._BALANCE_THRESHOLD_KOPECKS
                        and supplier_balance <= awaiting + self._BALANCE_THRESHOLD_KOPECKS):
                    pass   # аванс под открытый заказ — норма
                elif abs(supplier_balance) > self._BALANCE_THRESHOLD_KOPECKS:
                    problems.append(
                        ('переплата поставщику' if supplier_balance > 0
                         else 'приёмки не оплачены') +
                        f': {abs(supplier_balance) / 100:,.2f} ₽'.replace(',', ' '))
            awaiting_shipment = prepaid_unshipped.get(href, 0)
            if s['demands'] or s['paid_in']:
                if (customer_balance > 0 and is_marketplace(s['agent'])):
                    # маркетплейс платит реестром за период: долг по отгрузкам
                    # выравнивается следующей выплатой — это не потерянные деньги
                    pass
                elif (customer_balance < 0 and not on_commission
                        and -customer_balance <= awaiting_shipment + self._BALANCE_THRESHOLD_KOPECKS):
                    pass   # предоплата под открытый заказ покупателя — норма
                elif abs(customer_balance) > self._BALANCE_THRESHOLD_KOPECKS:
                    problems.append(
                        ('покупатель не доплатил' if customer_balance > 0
                         else 'получено больше, чем отгружено') +
                        f': {abs(customer_balance) / 100:,.2f} ₽'.replace(',', ' '))
            if not problems:
                continue
            entity_id = href.split('/')[-1].split('?')[0]
            out.append(RawFinding(
                entity_type='counterparty',
                entity_id=entity_id,
                entity_href=href,
                entity_name=s['agent'],
                severity=self.default_severity,
                payload={
                    'agent': s['agent'],
                    'signals': problems,
                    'supplies_rub': round(s['supplies'] / 100, 2),
                    'paid_out_rub': round(s['paid_out'] / 100, 2),
                    'demands_rub': round(s['demands'] / 100, 2),
                    'paid_in_rub': round(s['paid_in'] / 100, 2),
                    'on_commission': on_commission,
                    'sold_by_commission_rub': round(sold / 100, 2) if on_commission else None,
                    'goods_on_partner_shelf_rub': round(on_shelf / 100, 2) if on_commission else None,
                    'recent_docs': s['docs'][:15],
                    'open_purchase_orders': [
                        {'order': p['order'],
                         'awaiting_delivery_rub': round(p['awaiting_kopecks'] / 100, 2)}
                        for p in pending_orders.get(href, [])[:5]
                    ],
                    'open_customer_orders': pending_customer.get(href, [])[:5],
                    'paid_awaiting_shipment_rub': round(awaiting_shipment / 100, 2),
                    'note': ('Все суммы в этих данных УЖЕ В РУБЛЯХ. '
                             'Баланс рассчитан по всей истории документов. '
                             'Для поставщика долг = исходящие платежи меньше приёмок; '
                             'для покупателя = входящие платежи меньше отгрузок. '
                             'Не путай направление платежей в объяснении. '
                             'Переплата при открытом заказе поставщику = аванс (норма). '
                             'Полученное сверх отгруженного, покрытое оплаченным и ещё '
                             'не отгруженным заказом покупателя (paid_awaiting_shipment_rub), '
                             '= предоплата под заказ, тоже норма. '
                             'Долг покупателя, покрытый открытым заказом в статусе '
                             'ожидания оплаты или с договорённостью о постоплате '
                             'в комментарии, — норма, если договорённость свежая; '
                             'давний долг без движения — повод напомнить покупателю. '
                             'Для комиссионера (on_commission) долгом считается только '
                             'проданное по отчётам комиссионера; goods_on_partner_shelf_rub — '
                             'наш товар у него на реализации, это НЕ долг и не потеря.'),
                },
                # изменение баланса = новый сигнал; стабильный — молчит после ack
                fingerprint_salt=f'{supplier_balance}|{customer_balance}',
            ))
        return out

    def explain(self, payload: dict) -> str:
        return 'Расчётный баланс не сходится: ' + '; '.join(payload.get('signals', []))


class PaymentNoClosingDocsCheck(CheckSpec):
    """Исходящий платёж без привязки и без галки «Без закрывающих документов».

    Такой платёж навсегда повисает в балансе контрагента как «нам должны»
    (живой кейс платежей за доставку: галка выравнивает баланс). LLM по
    назначению отличает оплату услуг (нужна галка) от аванса за товар
    (привязка появится с приёмкой)."""

    id = 'payment_no_closing_docs'
    section = Section.MONEY
    title = 'Платёж без привязки и без галки «Без закрывающих документов»'
    default_severity = Severity.WARNING
    supports_incremental = False

    _GRACE_DAYS = 7   # свежий аванс ещё ждёт свою приёмку — не сигналим

    async def detect(self, ctx: AuditContext, since: datetime | None) -> list[RawFinding]:
        out = []
        for entity in ('paymentout', 'cashout'):
            docs = await ctx.cached_list(
                f'{entity}_3m', entity,
                filters=f'moment>={ctx.scan_since_moment}',
                expand='agent', order='moment,desc',
            )
            for d in docs:
                if d.get('applicable') is False:
                    continue
                if d.get('operations') or d.get('noClosingDocs'):
                    continue
                try:
                    age_days = (ctx.now - parse_moment(d['moment'])).days
                except (KeyError, ValueError):
                    continue
                if age_days < self._GRACE_DAYS:
                    continue
                out.append(RawFinding(
                    entity_type=entity,
                    entity_id=d['id'],
                    entity_href=d['meta']['href'],
                    entity_name=f'Платёж №{d["name"]} от {d["moment"][:10]}',
                    severity=self.default_severity,
                    payload={
                        'agent': ((d.get('agent') or {}).get('name')
                                  if isinstance(d.get('agent'), dict) else None),
                        'sum_kopecks': d.get('sum', 0),
                        'age_days': age_days,
                        'purpose': (d.get('paymentPurpose') or '')[:200],
                        'description': (d.get('description') or '')[:200],
                        'fix_hint': ('Если это оплата услуг (доставка, аренда, банк) — '
                                     'поставить галку «Без закрывающих документов» '
                                     '(set_no_closing_docs). Если аванс за товар — '
                                     'привязать к заказу/приёмке.'),
                    },
                    fingerprint_salt='',
                ))
        return out

    def explain(self, payload: dict) -> str:
        return (f'Платёж {payload.get("sum_kopecks", 0) / 100:,.2f} ₽ висит '
                f'{payload.get("age_days")} дн. без привязки и без галки «Без закрывающих '
                f'документов» — искажает баланс контрагента.').replace(',', ' ')
