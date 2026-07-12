"""Проверки раздела «Деньги»: дубли платежей."""

from collections import defaultdict
from datetime import datetime, timedelta

from services.audit.context import AuditContext, format_moment, parse_moment
from services.audit.specs import CheckSpec, RawFinding, Section, Severity

_DUPLICATE_WINDOW_DAYS = 14


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

    _BALANCE_THRESHOLD_KOPECKS = 100   # расхождения до 1 ₽ не сигналим

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

        # Недоплаченные заказы покупателей: долг при заказе в статусе ожидания
        # оплаты / с договорённостью о постоплате в комментарии — норма (кейс
        # Александровой: заказ «Ждем оплату», «оплата будет после 10го»).
        # Кодом не подавляем — судит LLM по статусу, комментарию и давности.
        pending_customer: dict[str, list] = {}
        customer_orders = await ctx.cached_list(
            'customerorder_balance_full', 'customerorder',
            expand='agent,state', order='moment,desc', max_rows=2000,
        )
        for o in customer_orders:
            if o.get('applicable') is False:
                continue
            unpaid = o.get('sum', 0) - o.get('payedSum', 0)
            if unpaid <= 0:
                continue
            agent = o.get('agent') if isinstance(o.get('agent'), dict) else None
            href = ((agent or {}).get('meta') or {}).get('href', '')
            if href:
                pending_customer.setdefault(href, []).append({
                    'order': f'Заказ покупателя №{o.get("name")} от {(o.get("moment") or "")[:10]}',
                    'awaiting_payment_rub': round(unpaid / 100, 2),
                    'status': ((o.get('state') or {}).get('name')) or None,
                    'comment': (o.get('description') or '')[:200],
                })

        out = []
        for href, s in acc.items():
            if not s['supplies'] and not s['demands']:
                # только платежи, товарных документов нет — операционные расходы
                # (банк, подписки, доставка); взаиморасчёт по товарам не применим
                continue
            supplier_balance = s['paid_out'] - s['supplies']   # >0 — переплатили поставщику
            customer_balance = s['demands'] - s['paid_in']     # >0 — нам должны
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
            if s['demands'] or s['paid_in']:
                if abs(customer_balance) > self._BALANCE_THRESHOLD_KOPECKS:
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
                    'recent_docs': s['docs'][:15],
                    'open_purchase_orders': [
                        {'order': p['order'],
                         'awaiting_delivery_rub': round(p['awaiting_kopecks'] / 100, 2)}
                        for p in pending_orders.get(href, [])[:5]
                    ],
                    'open_customer_orders': pending_customer.get(href, [])[:5],
                    'note': ('Все суммы в этих данных УЖЕ В РУБЛЯХ. '
                             'Баланс рассчитан по всей истории документов. '
                             'Для поставщика долг = исходящие платежи меньше приёмок; '
                             'для покупателя = входящие платежи меньше отгрузок. '
                             'Не путай направление платежей в объяснении. '
                             'Переплата при открытом заказе поставщику = аванс (норма). '
                             'Долг покупателя, покрытый открытым заказом в статусе '
                             'ожидания оплаты или с договорённостью о постоплате '
                             'в комментарии, — норма, если договорённость свежая; '
                             'давний долг без движения — повод напомнить покупателю.'),
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
