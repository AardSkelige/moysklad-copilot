"""Сквозные проверки: ретро-правки складских документов и зависшие черновики."""

from datetime import datetime, timedelta

from core import config
from core.logger import logger
from services.audit.context import AuditContext, format_moment, linked_documents, parse_moment
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

# связи разворачиваем только там, где они есть у сущности (иначе API отвечает 412)
_LINK_EXPAND = {'supply': 'purchaseOrder,payments'}

# Лимит запросов истории за прогон. Держать его низким выходит дороже, чем кажется:
# без истории документ нельзя отсеять как «правили только комментарий», и он уходит
# к LLM пустой карточкой «что менялось, не видно». Запрос к audit API дешевле и
# быстрее вызова LLM, поэтому потолок высокий (замер: 400 запросов ≈ 60 с,
# находок при этом 65 → 14).
_MAX_AUDIT_DIFFS = 300

# правки этих полей не трогают остатки и себестоимость: переписанный комментарий
# в проведённой приёмке — не то, ради чего будят владельца
_COSMETIC_FIELDS = {'description', 'name'}


def events_after(events: list[dict], after: str | None) -> list[dict]:
    """События ПОЗЖЕ указанного момента.

    Для ретро-правок интересны изменения после создания документа
    (или завершения производства), а не вся легитимная история ввода."""
    if not after:
        return list(events)
    return [ev for ev in events
            if not ((ev.get('moment') or '')[:16] and (ev.get('moment') or '')[:16] <= after[:16])]


def position_changed(change: dict) -> bool:
    """Позиция действительно изменилась.

    МойСклад пишет позицию в diff и когда её просто перезаписали теми же
    значениями («БТМС 6000 г по 1,39 ₽ → БТМС 6000 г по 1,39 ₽») — на остатки
    и FIFO это не влияет, но выглядит как правка состава."""
    old, new = change.get('oldValue'), change.get('newValue')
    if not (isinstance(old, dict) and isinstance(new, dict)):
        return True   # позицию добавили или удалили
    same_product = ((old.get('assortment') or {}).get('name')
                    == (new.get('assortment') or {}).get('name'))
    return not (same_product
                and all(old.get(k) == new.get(k) for k in ('quantity', 'price', 'uom')))


def meaningful_fields(diff: dict) -> set[str]:
    """Поля события, которые реально изменились."""
    out = set()
    for field, change in (diff or {}).items():
        if field == 'positions' and isinstance(change, list):
            if any(position_changed(c) for c in change if isinstance(c, dict)):
                out.add(field)
        else:
            out.add(field)
    return out


def is_cosmetic(events: list[dict]) -> bool:
    """Все правки — только комментарий/номер, на учёт не влияют."""
    return bool(events) and all(
        meaningful_fields(ev.get('diff') or {}) <= _COSMETIC_FIELDS for ev in events)


def last_meaningful_moment(events: list[dict]) -> str | None:
    """Момент последней правки, которая реально затронула учёт.

    Отпечаток находки строится на этой дате, а не на `updated` документа: иначе
    любое касание — включая правку комментария самим ревью комментариев — меняет
    отпечаток, и уже разобранная находка приходит владельцу заново."""
    moments = [(ev.get('moment') or '') for ev in events
               if not (meaningful_fields(ev.get('diff') or {}) <= _COSMETIC_FIELDS)]
    return max(moments) if moments else None


def _position_line(change: dict) -> str:
    """Строка позиции из diff — по-человечески, а не обрезанным JSON.

    Суммы в audit API уже в рублях (в отличие от документов), поэтому не делим."""
    old, new = change.get('oldValue'), change.get('newValue')

    def num(v) -> str:
        return f'{v:g}' if isinstance(v, (int, float)) else str(v)

    def descr(v: dict) -> str:
        name = ((v.get('assortment') or {}).get('name') or '?')
        price = v.get('price')
        price_s = f'{price:,.2f}'.replace(',', ' ').replace('.', ',') if isinstance(
            price, (int, float)) else str(price)
        return (f'{name} — {num(v.get("quantity"))} {v.get("uom") or ""}'
                f' по {price_s} ₽').replace('  ', ' ')

    if isinstance(old, dict) and isinstance(new, dict):
        return f'изменена позиция: {descr(old)} → {descr(new)}'
    if isinstance(new, dict):
        return f'добавлена позиция: {descr(new)}'
    if isinstance(old, dict):
        return f'удалена позиция: {descr(old)}'
    return str(change)[:120]


def _slim_audit_events(events: list[dict], limit: int = 5,
                       after: str | None = None) -> list[dict]:
    """Компактный diff событий audit API (не более limit штук)."""
    out = []
    for ev in events_after(events, after):
        if len(out) >= limit:
            break
        moment = (ev.get('moment') or '')[:16]
        diff = ev.get('diff') or {}
        slim_diff = {}
        for field, change in list(diff.items())[:6]:
            if isinstance(change, list):
                # positions приходит списком изменений — раскрываем, что за товар,
                # и выбрасываем перезаписи теми же значениями
                lines = [_position_line(c) for c in change
                         if isinstance(c, dict) and position_changed(c)]
                if not lines:
                    continue
                slim_diff[field] = lines[:8]
            elif isinstance(change, dict):
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
            'who': ev.get('uid', ''),   # кто правил — иначе LLM гадает по комментарию
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
                expand=_LINK_EXPAND.get(entity, ''),
                order='updated,desc',
            )
            for d in docs:
                if d.get('applicable') is False:
                    continue
                try:
                    # точка отсчёта — создание документа, а не его дата: приёмку от 04.08,
                    # заведённую 07.08, правкой считать нельзя — правок после создания не было
                    baseline = max(parse_moment(d['moment']),
                                   parse_moment(d.get('created') or d['moment']))
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
                        # интересуют только ПОЗДНИЕ правки: то, что доделали в первые
                        # сутки после создания, — обычное дозаполнение документа
                        raw_events = events_after(
                            await ctx.client.entity_audit_events(ctx.session, entity, d['id']),
                            format_moment(baseline + threshold),
                        )
                        history_checked = True
                        diffs_budget -= 1
                        if is_cosmetic(raw_events):
                            continue   # переписали только комментарий — на учёт не влияет
                        events = _slim_audit_events(raw_events)
                        salt_moment = (last_meaningful_moment(raw_events) or d['updated'])[:10]
                    except Exception:
                        # без истории находка звучит как «что-то поменяли, что именно —
                        # неизвестно»: обосновать её нечем, а владельца она будит.
                        # Пропускаем документ — вернёмся к нему следующим сканом
                        logger.warning(f'[audit] история {entity} {d["id"]} не получена, '
                                       f'ретро-правку отложил', exc_info=True)
                        continue
                out.append(RawFinding(
                    entity_type=entity,
                    entity_id=d['id'],
                    entity_href=d['meta']['href'],
                    entity_name=f'{label} №{d["name"]} от {d["moment"][:10]}',
                    severity=self.default_severity,
                    payload={
                        'doc_moment': (d.get('moment') or '')[:16],
                        'created': (d.get('created') or '')[:16],
                        'updated': (d.get('updated') or '')[:16],
                        'gap_days': round(gap.total_seconds() / 86400, 1),
                        'description': (d.get('description') or '')[:300],
                        'linked_documents': linked_documents(d),
                        'changes_after_doc_date': events,
                        # без флага пустая история читается как «правок не было» —
                        # хотя её могли просто не запросить (лимит запросов за прогон)
                        'history_checked': history_checked,
                    },
                    # новый сигнал даёт только новая ЗНАЧИМАЯ правка: причёсанный
                    # комментарий не должен присылать разобранную находку заново
                    fingerprint_salt=salt_moment,
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
                        # без этих двух полей нельзя отличить «доставку задвоили»
                        # от «доставка не попала в себестоимость»
                        'overhead_kopecks': (d.get('overhead') or {}).get('sum', 0) or 0,
                        'positions_sum_kopecks': d.get('sum', 0) or 0,
                        'note': ('Стандарт: доставка в закупке — накладные расходы '
                                 'документа, а не товарная позиция. Если накладные '
                                 'расходы (overhead_kopecks) НЕ нулевые, а позиция '
                                 '«Доставка» всё равно есть — доставка учтена дважды: '
                                 'в сумме документа и в себестоимости позиций. '
                                 'Если накладные нулевые — стоимость доставки в '
                                 'себестоимость сырья не попала, она занижена.'),
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
