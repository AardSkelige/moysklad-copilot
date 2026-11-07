from datetime import datetime, timedelta

from core.logger import logger
from integrations.moysklad_audit import MoySkladAuditClient
from services.audit.context import format_moment, is_marketplace
from integrations.moysklad_base import new_session
from services.audit.comment_review.common import (
    DEMAND_MARKETPLACE_ZERO_COMMENT,
    Progress,
    _too_fresh,
    needs_final_dot,
)
from services.audit.comment_review.documents import collect_documents


async def collect_dot_fixes(days: int) -> list[dict]:
    """Документы, которым не хватает только точки в конце — для пакетной правки.

    Собирается кодом, без LLM: правило механическое, а спрашивать по одному
    документу за точку — пустая трата внимания владельца."""
    docs, _ = await collect_documents(days)
    out = []
    for d in docs:
        for field, entity, entity_id, text in (
            ('comment', d['entity'], d['id'], d['comment']),
            ('order_comment', 'customerorder', d.get('order_id'), d.get('order_comment')),
        ):
            if not entity_id or not needs_final_dot(text):
                continue
            out.append({'entity': entity, 'id': entity_id,
                        'label': d['label'] if field == 'comment'
                                 else f'Заказ покупателя №{d.get("order_name")}',
                        'comment': text, 'new_comment': text.strip() + '.'})
    # заказ, связанный с двумя отгрузками, не должен попасть в список дважды
    seen, unique = set(), []
    for item in out:
        key = (item['entity'], item['id'])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


async def apply_batch_comments(items: list[dict], progress: Progress = None) -> int:
    """Записать комментарии пачкой (точки, маркетплейсы); вернуть число обновлённых."""
    client = MoySkladAuditClient()
    done = 0
    async with new_session() as session:
        for n, item in enumerate(items, 1):
            try:
                await client.update_entity(session, item['entity'], item['id'],
                                           {'description': item['new_comment']})
                done += 1
            except Exception as e:
                logger.warning(f'[comments] комментарий не записался в {item["label"]}: {e}')
            if progress:
                await progress(n, len(items))
    return done


async def collect_marketplace_zero_fixes(days: int) -> list[dict]:
    """Отгрузки маркетплейсу с нулевыми накладными и пустым комментарием —
    для пакетной правки одним стандартным текстом.

    Собирается кодом, без LLM: правило механическое (владелец подтвердил —
    «просто отгрузка», без разбора по одной). Трогаем только ПУСТОЙ
    комментарий — уже заполненный текст может нести содержательную причину
    и должен пройти обычное ревью, а не перезаписываться шаблоном."""
    client = MoySkladAuditClient()
    filters = (f'moment>={format_moment(datetime.now() - timedelta(days=days))}'
               if days > 0 else '')
    async with new_session() as session:
        docs = await client.list_entities(
            session, 'demand', filters=filters, expand='agent', order='moment,asc', max_rows=2000)
    out = []
    for d in docs:
        if d.get('applicable') is False:
            continue
        if (d.get('overhead') or {}).get('sum', 0) > 0:
            continue
        if (d.get('description') or '').strip():
            continue
        if _too_fresh(d.get('moment')):
            continue
        agent_name = ((d.get('agent') or {}).get('name')
                      if isinstance(d.get('agent'), dict) else None)
        if not is_marketplace(agent_name):
            continue
        out.append({'entity': 'demand', 'id': d['id'],
                    'label': f'Отгрузка №{d.get("name")} от {(d.get("moment") or "")[:10]} '
                             f'({agent_name})',
                    'comment': '', 'new_comment': DEMAND_MARKETPLACE_ZERO_COMMENT})
    return out
