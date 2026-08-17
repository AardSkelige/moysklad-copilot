"""Проверки раздела «Закупки»: приёмки и заказы поставщикам."""

from datetime import datetime, timedelta

from services.audit.context import AuditContext, format_moment
from services.audit.specs import CheckSpec, RawFinding, Section, Severity


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
            expand='positions.assortment,agent',
            order='moment,desc',
        )
        fifo = await ctx.stock_fifo_map()
        uoms = await ctx.uom_map()
        out = []
        for d in docs:
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

    # Статусы «поставка ещё в пути» — недопоставка в них норма, а не сигнал
    _AWAITING_MARKERS = ('заказан', 'ожидан', 'в пути', 'новый')

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
        grace = ctx.now - timedelta(days=7)   # свежим заказам даём время на приёмку
        for d in docs:
            total = d.get('sum', 0)
            shipped = d.get('shippedSum', 0)
            payed = d.get('payedSum', 0)
            moment = d.get('moment', '')[:19]
            is_old = moment < format_moment(grace)
            state = ((d.get('state') or {}).get('name') or '').strip()
            awaiting = any(m in state.lower() for m in self._AWAITING_MARKERS)
            problems = []
            if total and shipped < total and is_old and not awaiting:
                problems.append('приёмки меньше заказа')
            if total and shipped > total:
                problems.append('принято больше, чем заказано')
            if payed > total:
                problems.append('оплачено больше суммы заказа')
            if total and not d.get('supplies') and is_old and not awaiting:
                problems.append('нет ни одной приёмки')
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
