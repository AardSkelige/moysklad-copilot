"""Хэндлеры производственного агента — минимум кнопок, постоянный режим.

Поведение:
  1. Жмёт «🏭 Производство» в меню → state=waiting_input, чистая история.
  2. Текст или голос → агент уточняет/строит ПЗ. Несколько раундов диалога — нормально.
  3. Готов — превью с [✅ Создать] [❌ Отмена].
     • Создать → ПЗ-черновик в МС → state ОСТАЁТСЯ waiting_input, история обнуляется, бот ждёт следующее.
     • Отмена → state возвращается к waiting_input с пустой историей.
     • Если просто написать/сказать в режиме confirming — это уточнение, превью отменяется
       и текст уходит агенту как новый ход диалога (сохраняем историю).
  4. Выход из режима — только /cancel или возврат в главное меню через MAIN_MENU callback.
"""

import io

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.enums import ContentType

from core import config
from core.logger import logger
from services.production_agent import (
    ProductionAgent, preview_to_state, preview_from_state,
)
from services.production_task_service import (
    ProductionTaskService, StatusChangePreview, TaskPreview, TaskEditPreview,
)
from services.voice_service import VoiceService
from shared.constants import CallbackData
from shared.filters import IsProductionUserFilter
from shared.keyboards import (
    production_confirm_keyboard, main_menu_keyboard,
    production_exit_reply_keyboard, remove_reply_keyboard,
    PRODUCTION_EXIT_TEXT,
)
from shared.states import ProductionState

router = Router()
router.message.filter(IsProductionUserFilter())
router.callback_query.filter(IsProductionUserFilter())


# --- helpers ------------------------------------------------------------------

async def _reset_to_idle(state: FSMContext, author_name: str) -> None:
    """Очистить превью, обнулить историю, оставить в waiting_input."""
    history = ProductionAgent.init_history(author_name)
    await state.set_state(ProductionState.waiting_input)
    await state.set_data({'history': history})


async def _transcribe_voice(message: Message) -> str:
    bot = message.bot
    file = await bot.get_file(message.voice.file_id)
    buf = io.BytesIO()
    await bot.download_file(file.file_path, destination=buf)
    voice = VoiceService()
    return await voice.transcribe(buf.getvalue())


async def _process_user_text(message: Message, state: FSMContext, user_text: str):
    data = await state.get_data()
    history = data.get('history') or []
    agent = ProductionAgent()

    thinking = await message.answer('⏳ Думаю...')
    try:
        result = await agent.step(history, user_text)
    except Exception as e:
        logger.exception('agent step failed')
        try:
            await thinking.delete()
        except Exception:
            pass
        await message.answer(f'❌ Ошибка агента: {e}')
        return

    try:
        await thinking.delete()
    except Exception:
        pass

    await state.update_data(history=result['history'])

    if result['kind'] == 'reply':
        await message.answer(result['text'])
    elif result['kind'] == 'preview':
        preview = result['preview']
        await state.update_data(preview=preview_to_state(preview))
        await state.set_state(ProductionState.confirming)
        if isinstance(preview, StatusChangePreview):
            label = '✅ Сменить статус'
        elif isinstance(preview, TaskEditPreview):
            label = '✅ Применить'
        else:
            label = '✅ Создать'
        await message.answer(
            preview.to_telegram_message() + '\n\nИли просто скажи/напиши — что поменять.',
            reply_markup=production_confirm_keyboard(label),
        )
    else:
        await message.answer(f'⚠️ {result["text"]}')


# --- handlers -----------------------------------------------------------------

@router.callback_query(F.data == CallbackData.PRODUCTION_ENTRY)
async def production_entry(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    author_name = config.PRODUCTION_USERS.get(callback.from_user.id, 'Авто')
    await _reset_to_idle(state, author_name)
    await callback.message.answer(
        '🏭 <b>Производство</b>\n\n'
        '<b>Создать ПЗ:</b>\n'
        '• «10 шампуней универсальных на пятницу»\n'
        '• «5 кондиционеров Сибирский лес 500 мл к 15 июня»\n'
        '• «30 кг основы кондиционера к концу недели»\n\n'
        '<b>Сменить статус существующего:</b>\n'
        '• «переведи 305 в готово»\n'
        '• «поставь последнему обклеено»\n'
        '• «вчерашнее в выполнение»\n\n'
        '<b>Изменить позиции существующего:</b>\n'
        '• «добавь в 305 ещё 5 кондиционеров Сибирский лес»\n'
        '• «в последнем поменяй шампунь на 20 шт»\n'
        '• «убери из 305 репеллент 500»\n\n'
        'Можно текстом или голосом. Я остаюсь в режиме пока ты не нажмёшь кнопку снизу.',
        reply_markup=production_exit_reply_keyboard(),
    )


@router.message(ProductionState.waiting_input, F.content_type == ContentType.VOICE)
async def voice_in_input(message: Message, state: FSMContext):
    try:
        text = await _transcribe_voice(message)
    except Exception as e:
        logger.exception('voice transcription failed')
        await message.answer(f'❌ Не удалось расшифровать голос: {e}')
        return
    if not text:
        await message.answer('Не расслышал — повтори, пожалуйста.')
        return
    await message.answer(f'🎙 <i>{text}</i>')
    await _process_user_text(message, state, text)


@router.message(ProductionState.waiting_input, F.text, F.text != PRODUCTION_EXIT_TEXT)
async def text_in_input(message: Message, state: FSMContext):
    if message.text.strip().startswith('/'):
        return
    await _process_user_text(message, state, message.text.strip())


# В состоянии confirming — текст/голос трактуем как уточнение к превью.
# Сбрасываем превью, возвращаемся в waiting_input с сохранённой историей.

@router.message(ProductionState.confirming, F.content_type == ContentType.VOICE)
async def voice_in_confirming(message: Message, state: FSMContext):
    try:
        text = await _transcribe_voice(message)
    except Exception as e:
        logger.exception('voice transcription failed')
        await message.answer(f'❌ Не удалось расшифровать голос: {e}')
        return
    if not text:
        await message.answer('Не расслышал — повтори, пожалуйста.')
        return
    await message.answer(f'🎙 <i>{text}</i>')
    await state.update_data(preview=None)
    await state.set_state(ProductionState.waiting_input)
    await _process_user_text(message, state, text)


@router.message(ProductionState.confirming, F.text, F.text != PRODUCTION_EXIT_TEXT)
async def text_in_confirming(message: Message, state: FSMContext):
    if message.text.strip().startswith('/'):
        return
    await state.update_data(preview=None)
    await state.set_state(ProductionState.waiting_input)
    await _process_user_text(message, state, message.text.strip())


@router.callback_query(ProductionState.confirming, F.data == CallbackData.PRODUCTION_CONFIRM)
async def production_confirm(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    preview_state = data.get('preview')
    author_name = config.PRODUCTION_USERS.get(callback.from_user.id, 'Авто')

    if not preview_state:
        await callback.message.answer('⚠️ Нет данных для создания. Что произвести?')
        await _reset_to_idle(state, author_name)
        return

    preview = preview_from_state(preview_state)
    svc = ProductionTaskService()

    try:
        if isinstance(preview, StatusChangePreview):
            await svc.apply_status_change(preview)
            await callback.message.answer(
                f'✅ ПЗ № {preview.task_name}: статус «{preview.current_state_name}» → '
                f'«{preview.new_state_name}».\n\nЧто дальше?'
            )
        elif isinstance(preview, TaskEditPreview):
            summary = await svc.apply_edits(preview)
            parts = []
            if summary['added']:    parts.append(f'+{summary["added"]} поз.')
            if summary['updated']:  parts.append(f'~{summary["updated"]} поз.')
            if summary['deleted']:  parts.append(f'−{summary["deleted"]} поз.')
            details = ', '.join(parts) if parts else 'нет изменений'
            await callback.message.answer(
                f'✅ ПЗ № {preview.task_name} обновлено: {details}.\n\nЧто дальше?'
            )
        else:  # TaskPreview
            results = await svc.create_from_preview(preview)
            if len(results) == 1:
                lines = [f'✅ <b>ПЗ № {results[0].get("name", "?")}</b> создано как черновик.']
            else:
                lines = [f'✅ Создано <b>{len(results)} ПЗ</b> как черновики:']
                for r, t in zip(results, preview.tasks):
                    lines.append(f'   • № {r.get("name", "?")} — {t.description}')
            lines.append('Что ещё произвести?')
            await callback.message.answer('\n'.join(lines))
    except Exception as e:
        logger.exception('production action failed')
        await callback.message.answer(f'❌ Не удалось выполнить: {e}')
    finally:
        await _reset_to_idle(state, author_name)


@router.callback_query(ProductionState.confirming, F.data == CallbackData.PRODUCTION_CANCEL)
async def production_cancel_in_confirming(callback: CallbackQuery, state: FSMContext):
    """В режиме подтверждения [Отмена] = отменить ЭТО задание, остаться в режиме производства."""
    await callback.answer('Отменено')
    author_name = config.PRODUCTION_USERS.get(callback.from_user.id, 'Авто')
    await callback.message.answer(
        '❌ Задание отменено. Что произвести вместо этого?'
    )
    await _reset_to_idle(state, author_name)


async def _exit_mode(message: Message, state: FSMContext):
    await state.clear()
    # Сначала убираем reply-клавиатуру, затем показываем главное меню
    await message.answer('👋 Вышел из режима производства.', reply_markup=remove_reply_keyboard())
    await message.answer(
        'Главное меню:',
        reply_markup=main_menu_keyboard(user_id=message.from_user.id),
    )


@router.message(F.text == PRODUCTION_EXIT_TEXT)
async def btn_exit(message: Message, state: FSMContext):
    current = await state.get_state()
    if current and current.startswith('ProductionState'):
        await _exit_mode(message, state)

# /cancel обрабатывается глобально в handlers/common.py
