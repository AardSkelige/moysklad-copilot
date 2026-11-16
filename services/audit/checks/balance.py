"""Проверка раздела «Деньги»: расчётный баланс контрагента."""

from datetime import datetime

from services.audit.context import AuditContext, is_marketplace, parse_moment
from services.audit.specs import CheckSpec, RawFinding, Section, Severity
from services.audit.team_context import SETTLED_SUPPLY_GAPS

# по договору комиссионер оплачивает отчёт в течение месяца (Вест Поинт, 16.11.2026)
_COMMISSION_PAYMENT_DAYS = 30

# статусы заказа, при которых оплата не аванс, а деньги к возврату
_CANCELLED_ORDER_MARKERS = ('отмен', 'возврат')


def _is_cancelled_order(status: str) -> bool:
    low = (status or '').lower()
    return any(m in low for m in _CANCELLED_ORDER_MARKERS)


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
        async def _agent_sums(entity: str, key: str, acc: dict, sign: int = 1):
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
                    'docs': [], 'unpaid_supplies': [], 'goods_docs': 0,
                })
                amount = d.get('sum', 0)
                unpaid = amount - (d.get('payedSum') or 0)
                if entity == 'supply' and unpaid > 0:
                    settled = SETTLED_SUPPLY_GAPS.get(d.get('name'))
                    if settled:
                        amount -= unpaid
                    order_id = ((d.get('purchaseOrder') or {}).get('meta') or {}).get(
                        'href', '').split('/')[-1]
                    slot['unpaid_supplies'].append({
                        'supply': f'Приёмка №{d.get("name")} от {d.get("moment", "")[:10]}',
                        'unpaid_rub': round(unpaid / 100, 2),
                        'order_id': order_id,
                        'settled_by_owner': settled,
                    })
                slot[key] += sign * amount
                if key in ('supplies', 'demands'):
                    slot['goods_docs'] += 1
                slot['docs'].append(f'{entity} №{d.get("name")} {d.get("moment", "")[:10]} '
                                    f'{sign * d.get("sum", 0) / 100:.2f}')

        acc: dict[str, dict] = {}
        await _agent_sums('supply', 'supplies', acc)
        await _agent_sums('paymentout', 'paid_out', acc)
        await _agent_sums('cashout', 'paid_out', acc)
        await _agent_sums('demand', 'demands', acc)
        await _agent_sums('paymentin', 'paid_in', acc)
        await _agent_sums('cashin', 'paid_in', acc)
        await _agent_sums('salesreturn', 'demands', acc, sign=-1)
        await _agent_sums('purchasereturn', 'supplies', acc, sign=-1)

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
        # Нам комиссионер должен не выручку отчёта (sum), а сумму коммитента
        # (commitentSum = выручка − его вознаграждение). Живой кейс Каприоля
        # 29.09: сравнение с sum дало фантомный долг 140 тыс., из них 71 тыс. —
        # вознаграждение 30%, остальное — отчёты, заведённые накануне.
        sold_by_commission: dict[str, int] = {}
        owed_by_commission: dict[str, int] = {}
        unpaid_reports: dict[str, list] = {}
        not_yet_due: dict[str, int] = {}
        for r in await ctx.cached_list('commissionreportin_all', 'commissionreportin',
                                       expand='agent', order='moment,desc', max_rows=1000):
            if r.get('applicable') is False:
                continue
            href = ((r.get('agent') or {}).get('meta') or {}).get('href', '').split('?')[0]
            if not href:
                continue
            owed = r.get('commitentSum', 0)
            sold_by_commission[href] = sold_by_commission.get(href, 0) + r.get('sum', 0)
            owed_by_commission[href] = owed_by_commission.get(href, 0) + owed
            unpaid = owed - (r.get('payedSum') or 0)
            if unpaid > 0 and (ctx.now - parse_moment(r['moment'])).days < _COMMISSION_PAYMENT_DAYS:
                not_yet_due[href] = not_yet_due.get(href, 0) + unpaid
            if unpaid > 0:
                unpaid_reports.setdefault(href, []).append({
                    'report': f'Отчёт комиссионера №{r.get("name")} от {(r.get("moment") or "")[:10]}',
                    'awaiting_payment_rub': round(unpaid / 100, 2),
                })

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

        order_comments = {o['id']: (o.get('description') or '')[:200] for o in orders}

        adjustments: dict[str, int] = {}
        for a in await ctx.cached_list('counterpartyadjustment_all', 'counterpartyadjustment',
                                       expand='agent', order='moment,desc', max_rows=1000):
            if a.get('applicable') is False:
                continue
            href = ((a.get('agent') or {}).get('meta') or {}).get('href', '').split('?')[0]
            if href:
                adjustments[href] = adjustments.get(href, 0) + (a.get('sum') or 0)

        out = []
        for href, s in acc.items():
            if not s['goods_docs']:
                # только платежи, товарных документов нет — операционные расходы
                # (банк, подписки, доставка); взаиморасчёт по товарам не применим
                continue
            supplier_balance = s['paid_out'] - s['supplies']   # >0 — переплатили поставщику
            on_commission = href in commission_agents or href in sold_by_commission
            sold = sold_by_commission.get(href, 0)
            # у комиссионера долг — сумма коммитента по отчётам, а не отгрузка
            customer_base = owed_by_commission.get(href, 0) if on_commission else s['demands']
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
                elif (on_commission and customer_balance > 0
                        and customer_balance <= not_yet_due.get(href, 0) + self._BALANCE_THRESHOLD_KOPECKS):
                    pass   # срок оплаты отчёта комиссионера ещё не наступил
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
            adjustment = adjustments.get(href.split('?')[0], 0)
            net_balance = supplier_balance + customer_balance - adjustment
            if abs(net_balance) <= self._BALANCE_THRESHOLD_KOPECKS:
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
                    'adjustments_rub': round(adjustment / 100, 2),
                    'net_balance_rub': round(net_balance / 100, 2),
                    'on_commission': on_commission,
                    'sold_by_commission_rub': round(sold / 100, 2) if on_commission else None,
                    'goods_on_partner_shelf_rub': round(on_shelf / 100, 2) if on_commission else None,
                    'owed_by_commission_reports_rub': (
                        round(owed_by_commission.get(href, 0) / 100, 2) if on_commission else None),
                    'commission_reward_rub': (
                        round((sold - owed_by_commission.get(href, 0)) / 100, 2)
                        if on_commission else None),
                    'unpaid_commission_reports': unpaid_reports.get(href, [])[:5],
                    'recent_docs': s['docs'][:15],
                    'unpaid_supplies': [
                        {'supply': u['supply'], 'unpaid_rub': u['unpaid_rub'],
                         'order_comment': order_comments.get(u['order_id']) or None,
                         'settled_by_owner': u['settled_by_owner']}
                        for u in s['unpaid_supplies'][:10]
                    ],
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
                             'сумма к выплате по его отчётам (owed_by_commission_reports_rub = '
                             'продано минус его вознаграждение commission_reward_rub); '
                             'вознаграждение он оставляет себе, это НЕ долг. '
                             'goods_on_partner_shelf_rub — наш товар у него на реализации, '
                             'это тоже НЕ долг и не потеря. Неоплаченные отчёты перечислены '
                             'в unpaid_commission_reports: свежий отчёт (до ~2 недель) — '
                             'норма, выплата ещё в пути; давний — повод напомнить. '
                             'Долг поставщику раскладывай по unpaid_supplies: называй '
                             'конкретную приёмку и сумму недоплаты, а не перебирай документы. '
                             'order_comment — комментарий к заказу этой приёмки, часто '
                             'объясняет недоплату (доставка отдельным счётом). '
                             'settled_by_owner — владелец знает об этой недоплате и решил '
                             'её не править, в баланс она уже не входит.'),
                },
                # изменение баланса = новый сигнал; стабильный — молчит после ack
                fingerprint_salt=f'{supplier_balance}|{customer_balance}',
            ))
        return out

    def explain(self, payload: dict) -> str:
        return 'Расчётный баланс не сходится: ' + '; '.join(payload.get('signals', []))
