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
from services.audit.comment_review import (
    apply_comment, apply_demand, apply_finance, collect_documents,
    collect_finance_documents, refine_comment, refine_demand, refine_finance,
    review_documents, review_finance_documents,
)
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
        if is_finance:
            docs = await collect_finance_documents(days)
            suggestions = await review_finance_documents(docs)
        else:
            docs = await collect_documents(days)
            suggestions = await review_documents(docs)
    except Exception as e:
        logger.exception('comment review failed')
        await progress.edit_text(f'❌ Не получилось: {e}')
        return
    try:
        await progress.delete()
    except Exception:
        pass
    if not suggestions:
        await callback.message.answer(
            f'Проверил {len(docs)} документов — все комментарии в порядке 🎉')
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
