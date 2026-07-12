"""Общие команды — /start, главное меню"""

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from shared.constants import CallbackData
from shared.filters import IsBotUserFilter
from shared.keyboards import main_menu_keyboard, remove_reply_keyboard

router = Router()
router.message.filter(IsBotUserFilter())
router.callback_query.filter(IsBotUserFilter())


@router.message(Command('start'))
async def cmd_start(message: Message):
    await message.answer(
        '👋 <b>Корпоративный бот</b>\n\n'
        'Выберите действие:',
        reply_markup=main_menu_keyboard(user_id=message.from_user.id),
    )


@router.message(Command('cancel'))
async def cmd_cancel(message: Message, state: FSMContext):
    """Глобальная отмена: выходит из любого режима (производство, аудит, FSM финансов)."""
    current = await state.get_state()
    await state.clear()
    if current and current.startswith('ProductionState'):
        await message.answer('👋 Вышел из режима производства.',
                             reply_markup=remove_reply_keyboard())
    elif current and current.startswith('AuditState'):
        await message.answer('👋 Вышел из обсуждения находки.')
    elif current:
        await message.answer('❌ Действие отменено.')
    else:
        await message.answer('Нечего отменять 🙂')
    await message.answer(
        'Главное меню:',
        reply_markup=main_menu_keyboard(user_id=message.from_user.id),
    )


@router.callback_query(F.data == CallbackData.MAIN_MENU)
async def show_main_menu(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.answer(
        '👋 <b>Корпоративный бот</b>\n\n'
        'Выберите действие:',
        reply_markup=main_menu_keyboard(user_id=callback.from_user.id),
    )
