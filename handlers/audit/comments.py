"""Ревью комментариев: карточки «текущий / предлагаемый» с кнопками решения.

Свободный текст/голос при открытой карточке = указание боту доработать
предложение («упустил кондей», «допиши причину») — карточка обновляется.
"""

from aiogram import F, Router
from aiogram.enums import ContentType
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from core import config
from core.logger import logger
from services.audit import review_tracker
from services.audit.comment_review import (
    apply_comment, apply_demand, apply_dot_fixes, apply_finance, collect_documents,
    collect_dot_fixes, collect_finance_documents, refine_comment, refine_demand,
    refine_finance, review_documents, review_finance_documents,
)
from shared import session_scope
from shared.constants import CallbackData, CallbackPrefix
from shared.filters import IsAuditOwnerFilter
from shared.states import AuditState

router = Router()
router.message.filter(IsAuditOwnerFilter())
router.callback_query.filter(IsAuditOwnerFilter())


_MENU_EXPLAIN = (
    '💬 <b>Ревью комментариев</b>\n\n'
    'Два набора документов:\n'
    '📦 <b>Учёт</b> — приёмки, оприходования, списания, перемещения, инвентаризации, '
    'заказы, отгрузки, ПЗ. У отгрузки комментарий — только про накладные расходы, '
    'остальное бот предложит перенести в связанный заказ.\n'
    '💰 <b>Финансы</b> — платежи, ПКО/РКО. Проверяются два поля: '
    'назначение платежа и комментарий.\n\n'
    '📍 <b>Расставить точки</b> — комментарии, которым не хватает только точки '
    'в конце. Отдельными карточками они не показываются: правится пачкой.\n\n'
    'Выбери набор и период:'
)


def _period_keyboard() -> InlineKeyboardMarkup:
    days = config.AUDIT_COMMENT_REVIEW_DAYS
    go = CallbackPrefix.AUDIT_COMMENTS_GO
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f'📦 Учёт · {days} дн.', callback_data=f'{go}doc:{days}'),
            InlineKeyboardButton(text='📦 Учёт · вся история', callback_data=f'{go}doc:0'),
        ],
        [
            InlineKeyboardButton(text=f'💰 Финансы · {days} дн.', callback_data=f'{go}fin:{days}'),
            InlineKeyboardButton(text='💰 Финансы · вся история', callback_data=f'{go}fin:0'),
        ],
        [InlineKeyboardButton(text='📍 Расставить точки',
                              callback_data=CallbackData.AUDIT_COMMENT_DOTS)],
        [InlineKeyboardButton(text='◀️ Назад', callback_data=CallbackData.AUDIT_MENU)],
    ])


def _card_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text='✏️ Заменить', callback_data=CallbackData.AUDIT_COMMENT_APPLY),
            InlineKeyboardButton(text='👌 Оставить', callback_data=CallbackData.AUDIT_COMMENT_SKIP),
        ],
        [InlineKeyboardButton(text='🚪 Закончить', callback_data=CallbackData.AUDIT_COMMENT_STOP)],
    ])


def _card_text(item: dict, idx: int, total: int) -> str:
    agent = f' · {item["agent"]}' if item.get('agent') else ''
    header = (f'💬 <b>Документ {idx + 1} из {total}</b>\n'
              f'{item["label"]}{agent}\n\n')
    if item.get('kind') == 'finance':
        parts = []
        if item.get('new_purpose'):
            parts.append(f'<b>Назначение сейчас:</b>\n{item.get("purpose") or "<i>(пусто)</i>"}\n'
                         f'<b>Предлагаю:</b>\n{item["new_purpose"]}')
        if item.get('new_comment'):
            parts.append(f'<b>Комментарий сейчас:</b>\n{item.get("comment") or "<i>(пусто)</i>"}\n'
                         f'<b>Предлагаю:</b>\n{item["new_comment"]}')
        body = '\n\n'.join(parts)
    elif item.get('kind') == 'demand':
        parts = []
        if item.get('new_comment') is not None:
            proposed = item['new_comment'] or '🗑 <i>(удалить — всё нужное есть в заказе)</i>'
            parts.append(f'<b>Комментарий отгрузки сейчас:</b>\n'
                         f'{item.get("comment") or "<i>(пусто)</i>"}\n'
                         f'<b>Предлагаю:</b>\n{proposed}')
        if item.get('new_order_comment'):
            parts.append(f'<b>Комментарий заказа №{item.get("order_name")} сейчас:</b>\n'
                         f'{item.get("order_comment") or "<i>(пусто)</i>"}\n'
                         f'<b>Предлагаю:</b>\n{item["new_order_comment"]}')
        body = '\n\n'.join(parts)
    else:
        current = item.get('comment') or '<i>(пусто)</i>'
        body = (f'<b>Сейчас:</b>\n{current}\n\n'
                f'<b>Предлагаю:</b>\n{item["new_comment"]}')
    return (header + body +
            f'\n\n<i>{item.get("reason", "")}</i>\n\n'
            f'Или напиши/наговори, что поправить — переделаю.')


async def _show_card(message, state: FSMContext):
    data = await state.get_data()
    queue, idx = data.get('cmt_queue') or [], data.get('cmt_idx', 0)
    if idx >= len(queue):
        applied = data.get('cmt_applied', 0)
        skipped = data.get('cmt_skipped', 0)
        await state.clear()
        from shared.keyboards import audit_menu_keyboard
        await message.answer(
            f'✅ Ревью завершено: заменено {applied}, оставлено {skipped}.',
            reply_markup=audit_menu_keyboard(),
        )
        return
    try:
        async with session_scope() as session:
            await review_tracker.mark_shown(session, queue[idx])
    except Exception:
        logger.warning('[comments] не записал показ карточки', exc_info=True)
    await message.answer(_card_text(queue[idx], idx, len(queue)),
                         reply_markup=_card_keyboard())


@router.callback_query(F.data == CallbackData.AUDIT_COMMENTS)
async def on_comments_entry(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(_MENU_EXPLAIN, reply_markup=_period_keyboard())


@router.callback_query(F.data.in_({CallbackData.AUDIT_COMMENTS_30D,
                                   CallbackData.AUDIT_COMMENTS_ALL,
                                   CallbackData.AUDIT_COMMENTS_FIN}))
async def on_comments_legacy(callback: CallbackQuery):
    """Старые кнопки из прежних сообщений — ведут в новое меню."""
    await callback.answer()
    await callback.message.answer(_MENU_EXPLAIN, reply_markup=_period_keyboard())


@router.callback_query(F.data.startswith(CallbackPrefix.AUDIT_COMMENTS_GO))
async def on_comments_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        kind, days_str = callback.data[len(CallbackPrefix.AUDIT_COMMENTS_GO):].split(':')
        days = int(days_str)
    except ValueError:
        await callback.message.answer(_MENU_EXPLAIN, reply_markup=_period_keyboard())
        return
    is_finance = kind == 'fin'
    scope = (f'{"финансовые документы" if is_finance else "документы учёта"} '
             f'({"вся история" if days <= 0 else f"за {days} дн."}')
    progress = await callback.message.answer(
        f'💬 Собираю {scope}) и оцениваю по стандарту — '
        f'{"несколько минут" if days > 0 else "это займёт заметное время"}…'
    )
    try:
        docs, failed = await (collect_finance_documents(days) if is_finance
                              else collect_documents(days))
        # документ, показанный дважды и не изменившийся, больше не предлагаем
        async with session_scope() as session:
            docs = await review_tracker.filter_seen(session, docs)
        suggestions = await (review_finance_documents(docs) if is_finance
                             else review_documents(docs))
    except Exception as e:
        logger.exception('comment review failed')
        await progress.edit_text(f'❌ Не получилось: {e}',
                                 reply_markup=_period_keyboard())
        return
    try:
        await progress.delete()
    except Exception:
        pass
    if failed:
        # молчаливое «всё в порядке» после обрыва связи — худший из отчётов:
        # владелец решит, что проверка прошла, а её не было
        await callback.message.answer(
            '⚠️ МойСклад не отдал часть документов: ' + ', '.join(failed) +
            '.\nЭти типы НЕ проверены — стоит повторить позже.')
    if not failed and not suggestions:
        await callback.message.answer(
            f'Проверил {len(docs)} документов — все комментарии в порядке 🎉',
            reply_markup=_period_keyboard())
        return
    if not suggestions:
        await callback.message.answer(
            f'В собранных документах ({len(docs)}) правок не нашлось.',
            reply_markup=_period_keyboard())
        return
    await state.set_state(AuditState.reviewing_comments)
    await state.set_data({'cmt_queue': suggestions, 'cmt_idx': 0,
                          'cmt_applied': 0, 'cmt_skipped': 0})
    await callback.message.answer(
        f'Проверил {len(docs)} документов, предлагаю поправить {len(suggestions)}. '
        f'Иду по типам документов, от старых к новым:')
    await _show_card(callback.message, state)


@router.message(AuditState.reviewing_comments, F.text, ~F.text.startswith('/'))
async def refine_in_review(message: Message, state: FSMContext):
    """Текст при открытой карточке = указание доработать предложение."""
    await _refine_current(message, state, message.text.strip())


@router.message(AuditState.reviewing_comments, F.content_type == ContentType.VOICE)
async def refine_voice_in_review(message: Message, state: FSMContext):
    from handlers.audit.dialog import _transcribe_voice
    text = await _transcribe_voice(message)
    if text:
        await _refine_current(message, state, text)


async def _refine_current(message: Message, state: FSMContext, instruction: str):
    data = await state.get_data()
    queue, idx = data.get('cmt_queue') or [], data.get('cmt_idx', 0)
    if idx >= len(queue):
        return
    item = queue[idx]
    thinking = await message.answer('⏳ Переделываю…')
    try:
        if item.get('kind') == 'finance':
            item = await refine_finance(item, instruction)
        elif item.get('kind') == 'demand':
            item = await refine_demand(item, instruction)
        else:
            item['new_comment'] = await refine_comment(item, instruction)
        item['reason'] = f'учтено твоё замечание: {instruction[:120]}'
    except Exception as e:
        logger.exception('refine comment failed')
        await message.answer(f'❌ Не получилось: {e}')
        return
    finally:
        try:
            await thinking.delete()
        except Exception:
            pass
    queue[idx] = item
    await state.update_data(cmt_queue=queue)
    await message.answer(_card_text(item, idx, len(queue)),
                         reply_markup=_card_keyboard())


@router.callback_query(AuditState.reviewing_comments, F.data == CallbackData.AUDIT_COMMENT_APPLY)
async def on_comment_apply(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    queue, idx = data.get('cmt_queue') or [], data.get('cmt_idx', 0)
    if idx >= len(queue):
        await callback.answer()
        return
    item = queue[idx]
    try:
        if item.get('kind') == 'finance':
            await apply_finance(item)
        elif item.get('kind') == 'demand':
            await apply_demand(item)
        else:
            await apply_comment(item['entity'], item['id'], item['new_comment'])
    except Exception as e:
        logger.exception('apply comment failed')
        await callback.answer('Не удалось записать', show_alert=True)
        await callback.message.answer(f'❌ {item["label"]}: {e}')
        return
    try:
        # хэш записанного состояния: правка бота не обнуляет счётчик показов
        async with session_scope() as session:
            await review_tracker.record_applied(session, item)
    except Exception:
        logger.warning('[comments] не записал применённое состояние', exc_info=True)
    await callback.answer('✏️ Заменено')
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.update_data(cmt_idx=idx + 1, cmt_applied=data.get('cmt_applied', 0) + 1)
    await _show_card(callback.message, state)


@router.callback_query(AuditState.reviewing_comments, F.data == CallbackData.AUDIT_COMMENT_SKIP)
async def on_comment_skip(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await callback.answer('👌 Оставили как есть')
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.update_data(cmt_idx=data.get('cmt_idx', 0) + 1,
                            cmt_skipped=data.get('cmt_skipped', 0) + 1)
    await _show_card(callback.message, state)


@router.callback_query(F.data == CallbackData.AUDIT_COMMENT_STOP)
async def on_comment_stop(callback: CallbackQuery, state: FSMContext):
    """Без фильтра состояния — выход работает всегда."""
    await callback.answer()
    data = await state.get_data()
    applied = data.get('cmt_applied', 0)
    skipped = data.get('cmt_skipped', 0)
    current = await state.get_state()
    if current and current.startswith('AuditState'):
        await state.clear()
    from shared.keyboards import audit_menu_keyboard
    await callback.message.answer(
        f'👋 Закончили: заменено {applied}, оставлено {skipped}.',
        reply_markup=audit_menu_keyboard(),
    )


@router.callback_query(F.data == CallbackData.AUDIT_COMMENT_DOTS)
async def on_dots_preview(callback: CallbackQuery, state: FSMContext):
    """Список документов, которым не хватает только точки в конце."""
    await callback.answer()
    progress = await callback.message.answer('📍 Ищу комментарии без точки…')
    try:
        items = await collect_dot_fixes(config.AUDIT_COMMENT_REVIEW_DAYS)
    except Exception as e:
        logger.exception('dot fixes collect failed')
        await progress.edit_text(f'❌ Не получилось: {e}',
                                 reply_markup=_period_keyboard())
        return
    if not items:
        await progress.edit_text('Все комментарии заканчиваются точкой 🎉',
                                 reply_markup=_period_keyboard())
        return
    await state.update_data(cmt_dots=items)
    shown = '\n'.join(f'• {i["label"]} — {i["comment"][:60]}' for i in items[:15])
    tail = f'\n… и ещё {len(items) - 15}' if len(items) > 15 else ''
    await progress.edit_text(
        f'📍 <b>{len(items)} документов без точки в конце</b>\n\n{shown}{tail}\n\n'
        f'Поставить точку везде?',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text='✏️ Поставить точки',
                                 callback_data=CallbackData.AUDIT_COMMENT_DOTS_GO),
            InlineKeyboardButton(text='👌 Не надо', callback_data=CallbackData.AUDIT_COMMENTS),
        ]]),
    )


@router.callback_query(F.data == CallbackData.AUDIT_COMMENT_DOTS_GO)
async def on_dots_apply(callback: CallbackQuery, state: FSMContext):
    """Записать точки во все собранные документы."""
    await callback.answer()
    items = (await state.get_data()).get('cmt_dots') or []
    if not items:
        await callback.message.answer('Список устарел — собери заново.',
                                      reply_markup=_period_keyboard())
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    progress = await callback.message.answer(f'⏳ Ставлю точки: {len(items)} документов…')
    done = await apply_dot_fixes(items)
    await state.update_data(cmt_dots=[])
    from shared.keyboards import audit_menu_keyboard
    await progress.edit_text(f'✅ Точки поставлены: {done} из {len(items)}.',
                             reply_markup=audit_menu_keyboard())
