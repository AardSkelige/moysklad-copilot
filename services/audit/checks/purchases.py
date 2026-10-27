"""Проверки раздела «Закупки»: приёмки и заказы поставщикам."""

from datetime import datetime, timedelta

from services.audit.context import (
    AuditContext,
    format_moment,
    is_free_supplier,
    linked_documents,
)
from services.audit.humanize import format_rub
from services.audit.specs import CheckSpec, RawFinding, Section, Severity

PLAN_SLACK = timedelta(days=2)   # поставщики опаздывают на день-два от плановой даты


def _slim_doc(d: dict) -> dict:
    return {
        'name': d.get('name'),
        'moment': (d.get('moment') or '')[:16],
        'updated': (d.get('updated') or '')[:16],
        'sum_kopecks': d.get('sum', 0),
        'payed_kopecks': d.get('payedSum', 0),
        'agent': ((d.get('agent') or {}).get('name')
                  if isinstance(d.get('agent'), dict) else None),
        'description': (d.get('description') or '')[:300],
        'linked_documents': linked_documents(d),
    }


class SupplyZeroPriceCheck(CheckSpec):
    id = 'supply_zero_price'
    section = Section.PURCHASES
    title = 'Приёмка с нулевыми ценами позиций'
    default_severity = Severity.CRITICAL

    async def detect(self, ctx: AuditContext, since: datetime | None) -> list[RawFinding]:
        filters = (f'updated>={format_moment(since)}' if since
                   else f'moment>={ctx.scan_since_moment}')
        docs = await ctx.client.list_entities(
            ctx.session, 'supply',
            filters=filters + ';applicable=true',
            expand='positions.assortment,agent,purchaseOrder,payments',
            order='moment,desc',
        )
        fifo = await ctx.stock_fifo_map()
        uoms = await ctx.uom_map()
        out = []
        for d in docs:
            agent_name = ((d.get('agent') or {}).get('name')
                          if isinstance(d.get('agent'), dict) else None)
            if is_free_supplier(agent_name):
                continue   # типография печатает этикетки бесплатно — ноль штатен
            zero = []
            for p in d.get('positions', {}).get('rows', []):
                if p.get('price', 0) == 0:
                    a = p.get('assortment', {})
                    href = a.get('meta', {}).get('href', '').split('?')[0]
                    fifo_price, _ = fifo.get(href, (None, None))
                    zero.append({
                        'product': a.get('name', '?'),
                        'quantity': p.get('quantity', 0),
                        'uom': uoms.get(href, ''),
                        'current_fifo_kopecks': fifo_price,
                    })
            if zero:
                out.append(RawFinding(
                    entity_type='supply',
                    entity_id=d['id'],
                    entity_href=d['meta']['href'],
                    entity_name=f'Приёмка №{d["name"]} от {d["moment"][:10]}',
                    severity=self.default_severity,
                    payload={'doc': _slim_doc(d), 'zero_positions': zero,
                             'zero_count': len(zero)},
                    fingerprint_salt='|'.join(sorted(z['product'] for z in zero)),
                ))
        return out

    def explain(self, payload: dict) -> str:
        n = payload.get('zero_count', 0)
        return (f'{n} позиций принято по цене 0 — себестоимость готовой продукции '
                f'будет занижена (FIFO).')


class OrderSupplyMismatchCheck(CheckSpec):
    id = 'order_supply_mismatch'
    section = Section.PURCHASES
    title = 'Заказ поставщику расходится с приёмками/оплатой'

    async def detect(self, ctx: AuditContext, since: datetime | None) -> list[RawFinding]:
        filters = (f'updated>={format_moment(since)}' if since
                   else f'moment>={ctx.scan_since_moment}')
        docs = await ctx.client.list_entities(
            ctx.session, 'purchaseorder',
            filters=filters + ';applicable=true',
            expand='agent,state',
            order='moment,desc',
        )
        out = []
        for d in docs:
            total = d.get('sum', 0)
            shipped = d.get('shippedSum', 0)
            payed = d.get('payedSum', 0)
            state = ((d.get('state') or {}).get('name') or '').strip()
            problems = []
            if total and shipped > total:
                problems.append('принято больше, чем заказано')
            if payed > total:
                problems.append('оплачено больше суммы заказа')
            if not problems:
                continue
            out.append(RawFinding(
                entity_type='purchaseorder',
                entity_id=d['id'],
                entity_href=d['meta']['href'],
                entity_name=f'Заказ поставщику №{d["name"]} от {d["moment"][:10]}',
                severity=self.default_severity,
                payload={
                    'doc': _slim_doc(d),
                    'status': state or None,
                    'shipped_kopecks': shipped,
                    'supplies_count': len(d.get('supplies') or []),
                    'payments_count': len(d.get('payments') or []),
                    'signals': problems,
                },
                fingerprint_salt=f'{total}|{shipped}|{payed}',
            ))
        return out

    def explain(self, payload: dict) -> str:
        return 'Суммы заказа, приёмок и оплат не сходятся: ' + ', '.join(payload.get('signals', []))


class OrderSupplyOverdueCheck(CheckSpec):
    """Заказ принят не полностью, а плановая дата приёмки прошла или не указана."""

    id = 'order_supply_overdue'
    section = Section.PURCHASES
    title = 'Поставка по заказу не принята в срок'
    default_severity = Severity.WARNING
    supports_incremental = False   # срок истекает без правки документа
    llm_triage = False   # дата и суммы из документа, судить нечего

    _NO_PLAN_GRACE = timedelta(days=3)
    # статус ожидания спасает только заказ без плановой даты
    _AWAITING_MARKERS = ('заказан', 'ожидан', 'в пути', 'новый')

    async def detect(self, ctx: AuditContext, since: datetime | None) -> list[RawFinding]:
        docs = await ctx.client.list_entities(
            ctx.session, 'purchaseorder',
            filters=f'moment>={ctx.scan_since_moment};applicable=true',
            expand='agent,state',
            order='moment,desc',
        )
        out = []
        for d in docs:
            total = d.get('sum', 0)
            shipped = d.get('shippedSum', 0)
            if not total or shipped >= total:
                continue
            state = ((d.get('state') or {}).get('name') or '').strip()
            planned = (d.get('deliveryPlannedMoment') or '')[:19]
            if not planned and any(m in state.lower() for m in self._AWAITING_MARKERS):
                continue
            if planned:
                planned_at = datetime.fromisoformat(planned)
                if ctx.now <= planned_at + PLAN_SLACK:
                    continue
                overdue_days = (ctx.now - planned_at).days
                severity = Severity.IMPORTANT
            else:
                if ctx.now <= datetime.fromisoformat(d['moment'][:19]) + self._NO_PLAN_GRACE:
                    continue
                overdue_days = None
                severity = self.default_severity
            out.append(RawFinding(
                entity_type='purchaseorder',
                entity_id=d['id'],
                entity_href=d['meta']['href'],
                entity_name=f'Заказ поставщику №{d["name"]} от {d["moment"][:10]}',
                severity=severity,
                payload={
                    'doc': _slim_doc(d),
                    'status': state or None,
                    'shipped_kopecks': shipped,
                    'supplies_count': len(d.get('supplies') or []),
                    'planned_delivery': planned[:10] or None,
                    'overdue_days': overdue_days,
                },
                fingerprint_salt=planned[:10] or 'no_plan',
            ))
        return out

    def explain(self, payload: dict) -> str:
        doc = payload.get('doc', {})
        received = ('приёмки по нему нет' if not payload.get('supplies_count')
                    else f'принято {format_rub(payload.get("shipped_kopecks"))} '
                         f'из {format_rub(doc.get("sum_kopecks"))}')
        paid = (f'Заказ оплачен на {format_rub(doc.get("payed_kopecks"))}, '
                if doc.get('payed_kopecks') else 'Заказ не оплачен, ')
        planned = payload.get('planned_delivery')
        if not planned:
            return (f'{paid}{received}, а плановая дата приёмки не указана. '
                    f'Поставь в заказе дату, когда ждём поставку: до неё и ещё '
                    f'2 дня после агент будет молчать.')
        day, month, year = planned[8:10], planned[5:7], planned[:4]
        return (f'{paid}{received}. Плановая дата приёмки {day}.{month}.{year} '
                f'прошла {payload.get("overdue_days")} дн. назад. Если поставка задерживается, '
                f'сдвинь дату в заказе. Если товар уже пришёл, оформи приёмку.')
