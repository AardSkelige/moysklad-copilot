"""Диалог «Обсудить и исправить» по находке аудита.

Поток: [💬 Обсудить] → AuditState.discussing (история в FSM) → агент смотрит
документ/историю, предлагает варианты → prepare_fix → превью с
[✅ Применить] [❌ Отмена] → применение → finding = fixed.

Свободный текст в confirming_fix — уточнение: превью сбрасывается, текст
уходит агенту. Выход — кнопка «Закончить» или /cancel (без фильтра состояния).
"""

import io
import json

from aiogram import F, Router
from aiogram.enums import ContentType
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from core.logger import logger
from core.database import Finding, FindingStatus
from services.audit.fix_agent import FixAgent
from services.audit.fix_service import (
    ErrorFixService, fix_preview_from_state, fix_preview_to_state,
)
from shared import session_scope
from shared.constants import CallbackData, CallbackPrefix
from shared.filters import IsAuditOwnerFilter
from shared.states import AuditState

router = Router()
router.message.filter(IsAuditOwnerFilter())
router.callback_query.filter(IsAuditOwnerFilter())


def _fix_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='✅ Применить', callback_data=CallbackData.AUDIT_FIX_APPLY),
        InlineKeyboardButton(text='❌ Отмена', callback_data=CallbackData.AUDIT_FIX_CANCEL),
    ]])


def _exit_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='🚪 Закончить обсуждение',
                             callback_data=CallbackData.AUDIT_EXIT_DIALOG),
    ]])


async def _process_text(message: Message, state: FSMContext, user_text: str):
    data = await state.get_data()
    finding_id = data.get('audit_finding_id')
    history = data.get('audit_history') or []
    agent = FixAgent()

    thinking = await message.answer('⏳ Думаю...')
    try:
        result = await agent.step(finding_id, history, user_text)
    except Exception as e:
        logger.exception('fix agent step failed')
        try:
            await thinking.delete()
        except Exception:
            pass
        await message.answer(f'❌ Ошибка агента: {e}', reply_markup=_exit_keyboard())
        return
    try:
        await thinking.delete()
    except Exception:
        pass

    await state.update_data(audit_history=result['history'])

    if result['kind'] == 'reply':
        await message.answer(result['text'], reply_markup=_exit_keyboard())
    elif result['kind'] == 'preview':
        preview = result['preview']
        await state.update_data(audit_fix_preview=fix_preview_to_state(preview))
        await state.set_state(AuditState.confirming_fix)
        await message.answer(
            preview.to_telegram_message() + '\n\nИли напиши — что поменять.',
            reply_markup=_fix_confirm_keyboard(),
        )
    else:
        await message.answer(f'⚠️ {result["text"]}', reply_markup=_exit_keyboard())


@router.callback_query(F.data.startswith(CallbackPrefix.AUDIT_TALK))
async def on_talk(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        finding_id = int(callback.data[len(CallbackPrefix.AUDIT_TALK):])
    except ValueError:
        return
    async with session_scope() as db:
        finding = await db.get(Finding, finding_id)
        if finding is None:
            await callback.message.answer('Находка не найдена.')
            return
        db.expunge(finding)

    payload = json.loads(finding.payload or '{}')
    context = {
        'check': finding.title,
        'section': finding.section,
        'severity': finding.severity,
        'entity_type': finding.entity_type,
        'entity_id': finding.entity_id,
        'entity_name': finding.entity_name,
        **payload,
    }
    await state.set_state(AuditState.discussing)
    await state.set_data({
        'audit_finding_id': finding_id,
        'audit_history': FixAgent.init_history(context),
    })
    llm = payload.get('llm') or {}
    intro = llm.get('explanation') or finding.entity_name
    await callback.message.answer(
        f'💬 <b>Обсуждаем:</b> {finding.entity_name}\n\n{intro}\n\n'
        'Спрашивай или скажи, как исправить — я подготовлю изменение, '
        'применю только после твоего подтверждения.',
        reply_markup=_exit_keyboard(),
    )


async def _transcribe_voice(message: Message) -> str | None:
    """Расшифровать голосовое; None — не получилось (уже ответили пользователю)."""
    from core import config
    if not config.GROQ_API_KEY:
        await message.answer('Голос не настроен (нет GROQ_API_KEY) — напиши текстом.')
        return None
    from services.voice_service import VoiceService
    try:
        file = await message.bot.get_file(message.voice.file_id)
        buf = io.BytesIO()
        await message.bot.download_file(file.file_path, destination=buf)
        text = await VoiceService().transcribe(buf.getvalue())
    except Exception as e:
        logger.exception('audit voice transcription failed')
        await message.answer(f'❌ Не удалось расшифровать голос: {e}')
        return None
    if not text:
        await message.answer('Не расслышал — повтори, пожалуйста.')
        return None
    await message.answer(f'🎙 <i>{text}</i>')
    return text


async def _start_general_dialog(message: Message, state: FSMContext, text: str):
    await state.set_state(AuditState.discussing)
    await state.set_data({
        'audit_finding_id': None,
        'audit_history': FixAgent.init_general_history(),
    })
    await _process_text(message, state, text)


# ~startswith('/'): команды НЕ перехватываем — иначе /audit_findings и /audit
# молча проглатывались бы здесь и не доходили до своих хэндлеров ниже по цепочке
@router.message(AuditState.discussing, F.text, ~F.text.startswith('/'))
async def text_in_discussing(message: Message, state: FSMContext):
    await _process_text(message, state, message.text.strip())


@router.message(AuditState.discussing, F.content_type == ContentType.VOICE)
async def voice_in_discussing(message: Message, state: FSMContext):
    text = await _transcribe_voice(message)
    if text:
        await _process_text(message, state, text)


@router.message(AuditState.confirming_fix, F.content_type == ContentType.VOICE)
async def voice_in_confirming(message: Message, state: FSMContext):
    """Голос при показанном превью = уточнение, как и текст."""
    text = await _transcribe_voice(message)
    if not text:
        return
    await state.update_data(audit_fix_preview=None)
    await state.set_state(AuditState.discussing)
    await _process_text(message, state, text)


@router.message(StateFilter(None), F.text, ~F.text.startswith('/'))
async def free_text_assistant(message: Message, state: FSMContext):
    """Свободный текст вне режимов = вопрос ассистенту аудита.

    Роутер аудита подключён последним, так что финансы/производство
    перехватывают свои сообщения раньше; сюда попадает «просто текст» владельца."""
    await _start_general_dialog(message, state, message.text.strip())


@router.message(StateFilter(None), F.content_type == ContentType.VOICE)
async def free_voice_assistant(message: Message, state: FSMContext):
    text = await _transcribe_voice(message)
    if text:
        await _start_general_dialog(message, state, text)


@router.message(AuditState.confirming_fix, F.text, ~F.text.startswith('/'))
async def text_in_confirming(message: Message, state: FSMContext):
    """Свободный текст при показанном превью = уточнение: превью сбрасываем."""
    await state.update_data(audit_fix_preview=None)
    await state.set_state(AuditState.discussing)
    await _process_text(message, state, message.text.strip())


@router.callback_query(AuditState.confirming_fix, F.data == CallbackData.AUDIT_FIX_APPLY)
async def on_fix_apply(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    stored = data.get('audit_fix_preview')
    if not stored:
        await callback.message.answer('⚠️ Превью не найдено — опиши исправление ещё раз.')
        await state.set_state(AuditState.discussing)
        return
    preview = fix_preview_from_state(stored)
    try:
        results = await ErrorFixService().apply(preview)
    except Exception as e:
        logger.exception('fix apply failed')
        await callback.message.answer(f'❌ Не удалось применить: {e}',
                                      reply_markup=_exit_keyboard())
        await state.set_state(AuditState.discussing)
        return
    if preview.finding_id:
        async with session_scope() as db:
            finding = await db.get(Finding, preview.finding_id)
            if finding is not None:
                finding.status = FindingStatus.FIXED
    await state.clear()
    from shared.keyboards import audit_menu_keyboard
    tail = '\n\nНаходка закрыта. Продолжай разбор или спроси меня о чём угодно.' \
        if preview.finding_id else '\n\nСпроси о чём угодно или вернись в меню.'
    await callback.message.answer(
        '✅ Исправлено:\n' + '\n'.join(f'• {r}' for r in results) + tail,
        reply_markup=audit_menu_keyboard(),
    )


@router.callback_query(AuditState.confirming_fix, F.data == CallbackData.AUDIT_FIX_CANCEL)
async def on_fix_cancel(callback: CallbackQuery, state: FSMContext):
    await callback.answer('Отменено')
    await state.update_data(audit_fix_preview=None)
    await state.set_state(AuditState.discussing)
    await callback.message.answer('❌ Исправление отменено. Обсуждаем дальше?',
                                  reply_markup=_exit_keyboard())


@router.callback_query(F.data == CallbackData.AUDIT_EXIT_DIALOG)
async def on_exit_dialog(callback: CallbackQuery, state: FSMContext):
    """Выход из диалога — без фильтра состояния (работает из любого шага)."""
    await callback.answer()
    current = await state.get_state()
    if current and current.startswith('AuditState'):
        await state.clear()
    await callback.message.answer('👋 Закончили. Находка остаётся в списке (/audit_findings).')

# /cancel обрабатывается глобально в handlers/common.py