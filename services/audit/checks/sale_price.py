from datetime import datetime
from html import escape

from services.audit.context import AuditContext, format_moment
from services.audit.humanize import format_rub
from services.audit.specs import CheckSpec, RawFinding, Section, Severity

_TOO_HIGH = 3.0
_TOO_LOW = 0.3
_MAX_DEMANDS = 5000


def _times(value: float) -> str:
    return f'{value:.1f}'.replace('.', ',')


def _signal(price: float, retail: float) -> str | None:
    ratio = price / retail
    if ratio > _TOO_HIGH:
        return f'дороже розницы в {_times(ratio)} раза'
    if ratio < _TOO_LOW:
        return f'дешевле розницы в {_times(1 / ratio)} раза'
    return None


class DemandPriceVsRetailCheck(CheckSpec):
    id = 'demand_price_vs_retail'
    section = Section.SALES
    title = 'Цена в отгрузке не похожа на цену товара'
    llm_triage = False

    async def detect(self, ctx: AuditContext, since: datetime | None) -> list[RawFinding]:
        filters = (f'updated>={format_moment(since)}' if since
                   else f'moment>={ctx.scan_since_moment}')
        docs = await ctx.client.list_entities(
            ctx.session, 'demand',
            filters=filters + ';applicable=true',
            expand='positions.assortment,agent',
            order='moment,desc',
            max_rows=_MAX_DEMANDS,
        )
        out = []
        for d in docs:
            suspicious = []
            for p in d.get('positions', {}).get('rows', []):
                a = p.get('assortment') or {}
                if (a.get('meta') or {}).get('type') == 'service':
                    continue
                retail = ((a.get('salePrices') or [{}])[0]).get('value') or 0
                price = (p.get('price') or 0) * (1 - (p.get('discount') or 0) / 100)
                if not retail or not price:
                    continue
                signal = _signal(price, retail)
                if signal:
                    suspicious.append({
                        'product': a.get('name', '?'),
                        'price_kopecks': price,
                        'retail_kopecks': retail,
                        'signal': signal,
                    })
            if suspicious:
                agent = d.get('agent')
                out.append(RawFinding(
                    entity_type='demand',
                    entity_id=d['id'],
                    entity_href=d['meta']['href'],
                    entity_name=f'Отгрузка №{d["name"]} от {d["moment"][:10]}',
                    severity=Severity.IMPORTANT,
                    payload={
                        'moment': (d.get('moment') or '')[:16],
                        'agent': agent.get('name') if isinstance(agent, dict) else None,
                        'description': (d.get('description') or '')[:300],
                        'positions': suspicious,
                    },
                    fingerprint_salt='|'.join(sorted(
                        f'{s["product"]}:{s["price_kopecks"]:g}' for s in suspicious)),
                ))
        return out

    def explain(self, payload: dict) -> str:
        lines = '\n'.join(
            f'• {escape(p["product"])}: в отгрузке {format_rub(p["price_kopecks"])}, '
            f'розница {format_rub(p["retail_kopecks"])}, {p["signal"]}'
            for p in payload.get('positions', []))
        return (f'{lines}\n\nПохоже, в документ выбран не тот товар (другой объём) '
                f'или в цене опечатка. Сверь с заказом.')
