"""Учёт показов карточек ревью комментариев: документ попадается не больше MAX_SHOWS раз.

Счётчик привязан к состоянию текстов документа (state_hash): если комментарий
с последнего показа изменили руками, документ снова допускается к ревью со счётом
заново. Правка самого бота («Заменить») изменением не считается — после записи
сохраняется хэш нового состояния (record_applied).
"""

import hashlib
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import CommentReviewSeen

MAX_SHOWS = 2


def _normalized(text) -> str:
    return ' '.join((text or '').lower().split())


def _pick(doc: dict, applied: bool, new_key: str, cur_key: str):
    """Текущее поле документа; после записи (applied) — записанное значение."""
    if applied and doc.get(new_key) is not None:
        return doc[new_key]
    return doc.get(cur_key)


def _state_hash(doc: dict, applied: bool = False) -> str:
    if doc.get('kind') == 'demand':
        parts = [_pick(doc, applied, 'new_comment', 'comment'),
                 _pick(doc, applied, 'new_order_comment', 'order_comment')]
    elif doc.get('kind') == 'finance':
        parts = [_pick(doc, applied, 'new_purpose', 'purpose'),
                 _pick(doc, applied, 'new_comment', 'comment')]
    else:
        parts = [_pick(doc, applied, 'new_comment', 'comment')]
    raw = '\n'.join(_normalized(p) for p in parts)
    return hashlib.sha1(raw.encode()).hexdigest()


async def _get_row(session: AsyncSession, doc: dict) -> CommentReviewSeen | None:
    result = await session.execute(select(CommentReviewSeen).where(
        CommentReviewSeen.entity == doc['entity'],
        CommentReviewSeen.entity_id == doc['id'],
    ))
    return result.scalar_one_or_none()


async def filter_seen(session: AsyncSession, docs: list[dict]) -> list[dict]:
    """Убрать документы, уже показанные MAX_SHOWS раз и не изменившиеся с тех пор."""
    if not docs:
        return docs
    rows = (await session.execute(select(CommentReviewSeen))).scalars().all()
    seen = {(r.entity, r.entity_id): r for r in rows}
    out = []
    for d in docs:
        row = seen.get((d['entity'], d['id']))
        if row and row.shown_count >= MAX_SHOWS and row.state_hash == _state_hash(d):
            continue
        out.append(d)
    return out


async def mark_shown(session: AsyncSession, doc: dict):
    """Зафиксировать показ карточки; документ, изменённый с прошлого показа, считается заново."""
    state = _state_hash(doc)
    row = await _get_row(session, doc)
    if row is None:
        session.add(CommentReviewSeen(entity=doc['entity'], entity_id=doc['id'],
                                      shown_count=1, state_hash=state,
                                      last_shown_at=datetime.now()))
        return
    row.shown_count = 1 if row.state_hash != state else row.shown_count + 1
    row.state_hash = state
    row.last_shown_at = datetime.now()


async def record_applied(session: AsyncSession, item: dict):
    """После «Заменить» запомнить записанное состояние — правка бота не обнуляет счётчик."""
    row = await _get_row(session, item)
    if row is not None:
        row.state_hash = _state_hash(item, applied=True)
