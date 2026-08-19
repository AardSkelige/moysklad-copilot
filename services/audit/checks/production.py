"""Проверки раздела «Производство»: ретро-правки ПЗ и зависшие задания.

productiontask не поддерживает filter=applicable (API отвечает 412) —
фильтруем по полю в коде.
"""

from datetime import datetime, timedelta

from core import config
from core.logger import logger
from services.audit.checks.cross import (
    _slim_audit_events,
    events_after,
    last_meaningful_moment,
    meaningful_fields,
)
from services.audit.context import AuditContext, format_moment, parse_moment
from services.audit.specs import CheckSpec, RawFinding, Section, Severity

_DONE_STATES = {'готово'}

# у ПЗ статус — часть рабочего процесса («выполнение» → «готово»), на остатки
# и себестоимость он не влияет: их двигает состав материалов и выпуска
_HARMLESS_FIELDS = {'description', 'name', 'state'}


def is_cosmetic(events: list[dict]) -> bool:
    """Все правки ПЗ — комментарий, номер или статус."""
    return bool(events) and all(
        meaningful_fields(ev.get('diff') or {}) <= _HARMLESS_FIELDS for ev in events)


_MAX_AUDIT_DIFFS = 300   # см. комментарий в cross.py: без истории отсев косметики не работает


class ProductionRetroEditCheck(CheckSpec):
    """Выполненное ПЗ изменено после завершения производства — главный источник
    минусов по сырью: этап отменили/поменяли после того, как по нему уже отгрузили.

    Точка отсчёта — productionEnd, а не moment: ПЗ по природе выполняется
    позже даты создания, и правки ДО завершения — нормальный рабочий процесс."""

    id = 'production_retro_edit'
    section = Section.PRODUCTION
    title = 'Производственное задание изменено после выполнения'
    default_severity = Severity.CRITICAL

    async def detect(self, ctx: AuditContext, since: datetime | None) -> list[RawFinding]:
        window_start = format_moment(since) if since else ctx.scan_since_moment
        threshold = timedelta(hours=config.AUDIT_RETRO_EDIT_HOURS)
        docs = await ctx.client.list_entities(
            ctx.session, 'productiontask',
            filters=f'updated>={window_start}',
            expand='state',
            order='updated,desc',
        )
        out = []
        diffs_budget = _MAX_AUDIT_DIFFS
        for d in docs:
            if d.get('applicable') is False:
                continue
            production_end = d.get('productionEnd')
            if not production_end:
                continue   # производство не завершено — правки легальны
            try:
                # ПЗ, заведённое в МС уже после завершения производства, правкой не считается:
                # отсчёт от создания, иначе оформление постфактум само даёт разрыв
                baseline = max(parse_moment(production_end),
                               parse_moment(d.get('created') or production_end))
                gap = parse_moment(d['updated']) - baseline
            except (KeyError, ValueError):
                continue
            if gap <= threshold:
                continue
            events = []
            history_checked = False
            salt_moment = d['updated'][:10]
            if diffs_budget > 0:
                try:
                    # как и для складских документов: правки в первые сутки — дозаполнение
                    raw_events = events_after(
                        await ctx.client.entity_audit_events(ctx.session, 'productiontask', d['id']),
                        format_moment(baseline + threshold),
                    )
                    history_checked = True
                    diffs_budget -= 1
                    if is_cosmetic(raw_events):
                        continue   # правили только комментарий/номер — учёт не затронут
                    events = _slim_audit_events(raw_events)
                    # отпечаток — по последней значимой правке, иначе причёсанный
                    # комментарий присылает разобранную находку заново
                    salt_moment = (last_meaningful_moment(raw_events) or d['updated'])[:10]
                except Exception:
                    # без истории обосновать находку нечем — отложим до следующего скана
                    logger.warning(f'[audit] история ПЗ {d["id"]} не получена, '
                                   f'ретро-правку отложил', exc_info=True)
                    continue
            out.append(RawFinding(
                entity_type='productiontask',
                entity_id=d['id'],
                entity_href=d['meta']['href'],
                entity_name=f'ПЗ №{d["name"]} от {d["moment"][:10]}',
                severity=self.default_severity,
                payload={
                    'doc_moment': (d.get('moment') or '')[:16],
                    'production_end': (production_end or '')[:16],
                    'created': (d.get('created') or '')[:16],
                    'updated': (d.get('updated') or '')[:16],
                    'gap_days_after_completion': round(gap.total_seconds() / 86400, 1),
                    'state': (d.get('state') or {}).get('name', ''),
                    'description': (d.get('description') or '')[:300],
                    'changes_after_completion': events,
                    'history_checked': history_checked,
                },
                fingerprint_salt=salt_moment,
            ))
        return out

    def explain(self, payload: dict) -> str:
        return (f'ПЗ выполнено {payload.get("production_end")}, но изменено '
                f'{payload.get("updated")} (+{payload.get("gap_days_after_completion")} дн '
                f'после завершения). Правка уже выполненного производства '
                f'ломает остатки сырья и готовой продукции.')


class ProductionStuckCheck(CheckSpec):
    """ПЗ, которое не «Готово» дольше порога (кейс №00047 — «Ожидание» 54 дня)."""

    id = 'production_stuck'
    section = Section.PRODUCTION
    title = 'Производственное задание зависло'
    default_severity = Severity.WARNING
    supports_incremental = False

    async def detect(self, ctx: AuditContext, since: datetime | None) -> list[RawFinding]:
        docs = await ctx.client.list_entities(
            ctx.session, 'productiontask',
            expand='state',
            order='moment,desc',
            max_rows=300,
        )
        out = []
        for d in docs:
            state = ((d.get('state') or {}).get('name') or '').lower()
            if state in _DONE_STATES:
                continue
            try:
                age = (ctx.now - parse_moment(d['moment'])).days
            except (KeyError, ValueError):
                continue
            if age < config.AUDIT_PRODUCTION_STUCK_DAYS:
                continue
            out.append(RawFinding(
                entity_type='productiontask',
                entity_id=d['id'],
                entity_href=d['meta']['href'],
                entity_name=f'ПЗ №{d["name"]} от {d["moment"][:10]}',
                severity=self.default_severity,
                payload={
                    'state': (d.get('state') or {}).get('name', '—'),
                    'age_days': age,
                    'applicable': d.get('applicable'),
                    'description': (d.get('description') or '')[:300],
                },
                fingerprint_salt='',
            ))
        return out

    def explain(self, payload: dict) -> str:
        return (f'ПЗ в статусе «{payload.get("state")}» уже {payload.get("age_days")} дн. '
                f'Довести до «Готово» или закрыть/удалить.')
