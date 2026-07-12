"""Сквозные проверки: ретро-правки складских документов и зависшие черновики."""

from datetime import datetime, timedelta

from core import config
from services.audit.context import AuditContext, format_moment, parse_moment
from services.audit.specs import CheckSpec, RawFinding, Section, Severity

# Складские документы, где поздняя правка бьёт по остаткам и FIFO прошлых дат.
# Заказы покупателей и отгрузки намеренно исключены — их ретро-правки норма процесса.
_STOCK_ENTITIES = {
    'supply': 'Приёмка',
    'enter': 'Оприходование',
    'loss': 'Списание',
    'move': 'Перемещение',
    'inventory': 'Инвентаризация',
}

_MAX_AUDIT_DIFFS = 40   # лимит запросов diff за прогон — бережём лимиты API


def _slim_audit_events(events: list[dict], limit: int = 5,
                       after: str | None = None) -> list[dict]:
    """Компактный diff событий audit API.

    after — оставить только события ПОЗЖЕ этого момента: для ретро-правок
    интересны изменения после даты документа/завершения производства,
    а не вся легитимная история редактирования."""
    out = []
    for ev in events:
        if len(out) >= limit:
            break
        moment = (ev.get('moment') or '')[:16]
        if after and moment and moment <= after[:16]:
            continue
        diff = ev.get('diff') or {}
        slim_diff = {}
        for field, change in list(diff.items())[:6]:
            if isinstance(change, dict):
                old = change.get('oldValue')
                new = change.get('newValue')
                slim_diff[field] = {
                    'old': (old.get('name') if isinstance(old, dict) else old),
                    'new': (new.get('name') if isinstance(new, dict) else new),
                }
            else:
                slim_diff[field] = str(change)[:120]
        out.append({
            'moment': moment,
            'type': ev.get('eventType'),
            'diff': slim_diff,
        })
    return out


class RetroEditCheck(CheckSpec):
    """Проведённый складской документ изменён спустя >24 ч после своей даты.

    Принятое правило: проведённые складские документы спустя сутки не правим — это ломает
    FIFO и остатки на прошедшие даты. diff «что менялось» тянем из audit API."""

    id = 'retro_edit_stock'
    section = Section.CROSS
    title = 'Поздняя правка проведённого складского документа'

    async def detect(self, ctx: AuditContext, since: datetime | None) -> list[RawFinding]:
        window_start = format_moment(since) if since else ctx.scan_since_moment
        threshold = timedelta(hours=config.AUDIT_RETRO_EDIT_HOURS)
        out = []
        diffs_budget = _MAX_AUDIT_DIFFS
        for entity, label in _STOCK_ENTITIES.items():
            docs = await ctx.client.list_entities(
                ctx.session, entity,
                filters=f'updated>={window_start}',
                order='updated,desc',
            )
            for d in docs:
                if d.get('applicable') is False:
                    continue
                try:
                    gap = parse_moment(d['updated']) - parse_moment(d['moment'])
                except (KeyError, ValueError):
                    continue
                if gap <= threshold:
                    continue
                events = []
                if diffs_budget > 0:
                    try:
                        events = _slim_audit_events(
                            await ctx.client.entity_audit_events(ctx.session, entity, d['id']),
                            after=d.get('moment'),
                        )
                        diffs_budget -= 1
                    except Exception:
                        pass
                out.append(RawFinding(
                    entity_type=entity,
                    entity_id=d['id'],
                    entity_href=d['meta']['href'],
                    entity_name=f'{label} №{d["name"]} от {d["moment"][:10]}',
                    severity=self.default_severity,
                    payload={
                        'doc_moment': (d.get('moment') or '')[:16],
                        'updated': (d.get('updated') or '')[:16],
                        'gap_days': round(gap.total_seconds() / 86400, 1),
                        'description': (d.get('description') or '')[:300],
                        'changes_after_doc_date': events,
                    },
                    fingerprint_salt=d['updated'][:10],   # правка в другой день = новый сигнал
                ))
        return out

    def explain(self, payload: dict) -> str:
        return (f'Документ от {payload.get("doc_moment")} изменён {payload.get("updated")} '
                f'(+{payload.get("gap_days")} дн). Правка старого документа пересчитывает '
                f'FIFO и остатки на прошедшие даты.')


class DeliveryAsPositionCheck(CheckSpec):
    """Доставка добавлена товарной позицией в закупочный документ.

    Принятый стандарт: в закупках доставка идёт накладными расходами приёмки,
    позицией её быть не должно. Продажи (заказы покупателей, отгрузки)
    НЕ проверяем — там своя сложная логика доставки, решение владельца."""

    id = 'delivery_as_position'
    section = Section.PURCHASES
    title = 'Доставка добавлена позицией в закупочный документ'

    _ENTITIES = {
        'purchaseorder': 'Заказ поставщику',
        'supply': 'Приёмка',
    }

    async def detect(self, ctx: AuditContext, since: datetime | None) -> list[RawFinding]:
        filters = (f'updated>={format_moment(since)}' if since
                   else f'moment>={ctx.scan_since_moment}')
        out = []
        for entity, label in self._ENTITIES.items():
            docs = await ctx.client.list_entities(
                ctx.session, entity,
                filters=filters,
                expand='positions.assortment',
                order='moment,desc',
            )
            for d in docs:
                delivery = []
                for p in d.get('positions', {}).get('rows', []):
                    a = p.get('assortment', {})
                    name = (a.get('name') or '').lower()
                    if 'доставк' in name:
                        delivery.append({
                            'position': a.get('name'),
                            'quantity': p.get('quantity'),
                            'price_kopecks': p.get('price'),
                        })
                if not delivery:
                    continue
                out.append(RawFinding(
                    entity_type=entity,
                    entity_id=d['id'],
                    entity_href=d['meta']['href'],
                    entity_name=f'{label} №{d["name"]} от {d["moment"][:10]}',
                    severity=self.default_severity,
                    payload={
                        'kind': label,
                        'moment': (d.get('moment') or '')[:16],
                        'description': (d.get('description') or '')[:300],
                        'delivery_positions': delivery,
                    },
                    fingerprint_salt='',
                ))
        return out

    def explain(self, payload: dict) -> str:
        return ('В закупочном документе доставка оформлена товарной позицией. '
                'По стандарту она идёт накладными расходами приёмки — '
                'позиция-доставка искажает себестоимость и баланс поставщика.')


class StaleDocsCheck(CheckSpec):
    """Непроведённые черновики, зависшие дольше порога."""

    id = 'stale_drafts'
    section = Section.CROSS
    title = 'Зависший черновик'
    default_severity = Severity.WARNING
    supports_incremental = False   # «staleness» ищется только полным сканом

    _ENTITIES = {
        'supply': ('приёмка', 'AUDIT_STALE_DRAFT_DAYS'),
        'enter': ('оприходование', 'AUDIT_STALE_DRAFT_DAYS'),
        'loss': ('списание', 'AUDIT_STALE_DRAFT_DAYS'),
        'move': ('перемещение', 'AUDIT_STALE_DRAFT_DAYS'),
        'salesreturn': ('возврат покупателя', 'AUDIT_STALE_DRAFT_DAYS'),
        'purchaseorder': ('заказ поставщику', 'AUDIT_STALE_ORDER_DAYS'),
        'customerorder': ('заказ покупателя', 'AUDIT_STALE_ORDER_DAYS'),
    }

    async def detect(self, ctx: AuditContext, since: datetime | None) -> list[RawFinding]:
        out = []
        for entity, (label, cfg_name) in self._ENTITIES.items():
            threshold_days = getattr(config, cfg_name)
            docs = await ctx.client.list_entities(
                ctx.session, entity,
                filters='applicable=false',
                order='updated,desc',
            )
            for d in docs:
                try:
                    age = (ctx.now - parse_moment(d['updated'])).days
                except (KeyError, ValueError):
                    continue
                if age < threshold_days:
                    continue
                out.append(RawFinding(
                    entity_type=entity,
                    entity_id=d['id'],
                    entity_href=d['meta']['href'],
                    entity_name=f'Черновик: {label} №{d["name"]} от {d["moment"][:10]}',
                    severity=self.default_severity,
                    payload={
                        'kind': label,
                        'age_days': age,
                        'sum_kopecks': d.get('sum', 0),
                        'description': (d.get('description') or '')[:300],
                    },
                    fingerprint_salt='',
                ))
        return out

    def explain(self, payload: dict) -> str:
        return (f'Черновик ({payload.get("kind")}) не проведён {payload.get("age_days")} дн. '
                f'Либо провести, либо удалить — иначе висит и путает учёт.')
