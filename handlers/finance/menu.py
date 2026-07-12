"""Вход в раздел «Финансы» из главного меню"""

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from shared.constants import CallbackData
from shared.filters import IsAdminFilter
from shared.keyboards import finance_menu_keyboard

router = Router()
router.callback_query.filter(IsAdminFilter())


@router.callback_query(F.data == CallbackData.FINANCE_MENU)
async def show_finance_menu(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.answer(
        '💰 <b>Финансы</b>\n\nВыберите действие:',
        reply_markup=finance_menu_keyboard(),
    )
