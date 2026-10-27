"""Проверки раздела «Производство»: ретро-правки ПЗ и зависшие задания.

productiontask не поддерживает filter=applicable (API отвечает 412) —
фильтруем по полю в коде.
"""

from datetime import datetime, timedelta

from core import config
from core.logger import logger
from services.audit.checks.cross import (
    _slim_audit_events,
    comment_author,
    events_after,
    last_meaningful_moment,
    meaningful_fields,
)
from services.audit.checks.purchases import PLAN_SLACK
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
                    'signed_by': comment_author(d.get('description')),
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


async def _awaited_supplies(ctx: AuditContext) -> dict[str, list[dict]]:
    """Для каждого товара заказы поставщику, по которым он ещё не принят."""
    orders = await ctx.cached_list(
        'purchaseorders_scan', 'purchaseorder',
        filters=f'moment>={ctx.scan_since_moment};applicable=true', order='moment,desc')
    out: dict[str, list[dict]] = {}
    for o in orders:
        if o.get('shippedSum', 0) >= o.get('sum', 0):
            continue
        planned = (o.get('deliveryPlannedMoment') or '')[:19] or None
        for p in await ctx.client.get_positions(ctx.session, 'purchaseorder', o['id']):
            if p.get('shipped', 0) >= p.get('quantity', 0):
                continue
            href = p['assortment']['meta']['href'].split('?')[0]
            out.setdefault(href, []).append({'order': o['name'], 'planned': planned})
    return out


def _arrives_in_time(order: dict, now: datetime) -> bool:
    return bool(order['planned']) and now <= datetime.fromisoformat(order['planned']) + PLAN_SLACK


class ProductionStuckCheck(CheckSpec):
    """ПЗ, которое не «Готово» дольше порога (кейс №00047 — «Ожидание» 54 дня)."""

    # молчим, только если весь недостающий материал едет по заказу и дата не прошла

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
        awaited = None
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
            stock = await ctx.stock_fifo_map()
            short, in_stock = [], []
            for m in await ctx.client.production_materials(ctx.session, d['id']):
                have = stock.get(m['href'], (0, 0))[1]
                label = f'{m["code"]} {m["name"]}' if m.get('code') else m['name']
                if have >= m['need']:
                    in_stock.append(label)
                else:
                    short.append({'href': m['href'], 'material': label,
                                  'need': m['need'], 'in_stock': have})
            if short:
                if awaited is None:
                    awaited = await _awaited_supplies(ctx)
                for s in short:
                    s['awaited_orders'] = awaited.get(s.pop('href'), [])
                if all(any(_arrives_in_time(o, ctx.now) for o in s['awaited_orders'])
                       for s in short):
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
                    'short_materials': short,
                    'materials_in_stock': in_stock,
                },
                fingerprint_salt='',
            ))
        return out

    def explain(self, payload: dict) -> str:
        text = f'ПЗ в статусе «{payload.get("state")}» уже {payload.get("age_days")} дн.'
        short = payload.get('short_materials') or []
        if short:
            text += ' Не хватает: ' + '; '.join(
                f'{s["material"]} (нужно {s["need"]:g}, на складе {s["in_stock"]:g})'
                for s in short) + '.'
        elif payload.get('materials_in_stock'):
            text += ' Все материалы на складе есть.'
        return text + ' Довести до «Готово» или закрыть/удалить.'
