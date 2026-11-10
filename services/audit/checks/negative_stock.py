from datetime import datetime, timedelta
from html import escape

from core import config
from core.logger import logger
from services.audit.checks.cross import _slim_audit_events, events_after, meaningful_fields
from services.audit.context import AuditContext, format_moment, parse_moment
from services.audit.specs import CheckSpec, RawFinding, Section, Severity

_HISTORY_DAYS = 45
_SHOWN_OPERATIONS = 6
_CHECKED_OPERATIONS = 25
_STOCK_FIELDS = {'positions', 'materials', 'products', 'productionVolume',
                 'store', 'applicable', 'moment'}

_LABELS = {
    'demand': 'Отгрузка',
    'supply': 'Приёмка',
    'enter': 'Оприходование',
    'loss': 'Списание',
    'move': 'Перемещение',
    'salesreturn': 'Возврат покупателя',
    'purchasereturn': 'Возврат поставщику',
    'retaildemand': 'Розничная продажа',
    'productionstagecompletion': 'Выполнение этапа',
    'processing': 'Техоперация',
}


def _short(moment: str) -> str:
    return f'{moment[8:10]}.{moment[5:7]} {moment[11:16]}'


def _quantity(value: float) -> str:
    return f'{"+" if value > 0 else "−"}{abs(value):g}'


def _change_lines(events: list[dict]) -> list[str]:
    lines = []
    for event in events:
        for field, change in event['diff'].items():
            if isinstance(change, list):
                lines.extend(change)
            elif isinstance(change, dict):
                lines.append(f'{field}: {change.get("old")} → {change.get("new")}')
            else:
                lines.append(f'{field}: {change}')
    return lines


class NegativeStockCheck(CheckSpec):
    id = 'negative_stock'
    section = Section.WAREHOUSE
    title = 'Отрицательный остаток на складе'
    default_severity = Severity.CRITICAL
    llm_triage = False

    async def detect(self, ctx: AuditContext, since: datetime | None) -> list[RawFinding]:
        out = []
        for row in await ctx.client.stock_by_store_negative(ctx.session):
            href = row.get('meta', {}).get('href', '').split('?')[0]
            negative = [s for s in row.get('stockByStore', []) if s.get('stock', 0) < 0]
            if not negative:
                continue
            product = await ctx.client.get_by_href(ctx.session, href) or {}
            name = product.get('name') or '?'
            for s in negative:
                store = s.get('name', '?')
                out.append(RawFinding(
                    entity_type='product',
                    entity_id=href.split('/')[-1],
                    entity_href=href,
                    entity_name=f'{name} — склад «{store}»',
                    severity=self.default_severity,
                    payload={'product': name, 'store': store, 'stock': s.get('stock'),
                             **await self._history(ctx, href, store)},
                    fingerprint_salt=store,
                    ui_link=(product.get('meta') or {}).get('uuidHref', ''),
                ))
        return out

    async def _history(self, ctx: AuditContext, product_href: str, store: str) -> dict:
        try:
            since = format_moment(ctx.now - timedelta(days=_HISTORY_DAYS))
            rows = await ctx.client.product_operations(ctx.session, product_href, since)
            rows = sorted((r for r in rows if (r.get('store') or {}).get('name') == store),
                          key=lambda r: r['operation'].get('moment') or '')[-_CHECKED_OPERATIONS:]
            operations, late_edits = [], []
            for r in rows:
                op = r['operation']
                entity = op['meta']['type']
                title = f'{_LABELS.get(entity, "Документ")} №{op.get("name")}'
                operations.append(
                    f'{_short(op.get("moment") or "")} {title}: {_quantity(r.get("quantity", 0))}')
                edit = await self._late_edit(ctx, entity, op['meta']['href'].split('?')[0])
                if edit:
                    late_edits.append({'document': f'{title} от {(op.get("moment") or "")[8:10]}.'
                                                   f'{(op.get("moment") or "")[5:7]}', **edit})
            return {'recent_operations': operations[-_SHOWN_OPERATIONS:], 'late_edits': late_edits,
                    'history_checked': True}
        except Exception:
            logger.warning(f'[audit] история движений {product_href} не получена', exc_info=True)
            return {'recent_operations': [], 'late_edits': [], 'history_checked': False}

    async def _late_edit(self, ctx: AuditContext, entity: str, href: str) -> dict | None:
        doc = await ctx.client.get_by_href(ctx.session, href)
        if not doc or not doc.get('updated'):
            return None
        baseline = max(parse_moment(doc['moment']), parse_moment(doc.get('created') or doc['moment']))
        threshold = timedelta(hours=config.AUDIT_RETRO_EDIT_HOURS)
        if parse_moment(doc['updated']) - baseline <= threshold:
            return None
        events = []
        for event in events_after(
                await ctx.client.entity_audit_events(ctx.session, entity, doc['id']),
                format_moment(baseline + threshold)):
            fields = meaningful_fields(event.get('diff') or {}) & _STOCK_FIELDS
            if fields:
                events.append({**event, 'diff': {f: event['diff'][f] for f in fields}})
        changes = _change_lines(_slim_audit_events(events))
        if not changes:
            return None
        return {'edited': _short(max(e.get('moment') or '' for e in events)), 'changes': changes}

    def explain(self, payload: dict) -> str:
        text = (f'Остаток {payload.get("stock"):g} на складе «{escape(str(payload.get("store")))}»: '
                f'продано, списано или израсходовано больше, чем числится.')
        if not payload.get('history_checked'):
            return text + '\n\nИсторию движений в этот раз получить не удалось.'
        operations = payload.get('recent_operations') or []
        if operations:
            text += '\n\n<b>Последние движения:</b>\n' + '\n'.join(
                f'• {escape(line)}' for line in operations)
        edits = payload.get('late_edits') or []
        if edits:
            text += '\n\n<b>Правки старых документов:</b>\n' + '\n'.join(
                f'• {escape(e["document"])}, правка {e["edited"]}: '
                f'{escape("; ".join(e["changes"]))}' for e in edits)
        elif operations:
            text += ('\n\nПоздних правок в документах по товару не нашёл. '
                     'Проверь, внесён ли приход: розлив, приёмка или перемещение.')
        return text
