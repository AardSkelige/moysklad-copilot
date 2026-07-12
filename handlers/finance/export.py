"""Экспорт операций в Excel"""

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery

from core.logger import logger
from integrations.excel import generate_excel_from_ms
from integrations.moysklad import MoySkladClient
from shared import session_scope
from shared.constants import CallbackData
from shared.filters import IsAdminFilter
from shared.keyboards import finance_menu_keyboard

router = Router()
router.callback_query.filter(IsAdminFilter())

_ALL_RECORDS = 10_000


async def _build_category_detail_map(session) -> dict:
    """Map expense_item_href -> {'name': str, 'group': str}"""
    from sqlalchemy import select
    from core.database import Category
    result = await session.execute(select(Category))
    cats = result.scalars().all()
    return {
        cat.moysklad_expense_item_href: {'name': cat.name, 'group': cat.group}
        for cat in cats
        if cat.moysklad_expense_item_href
    }


@router.callback_query(F.data == CallbackData.FINANCE_EXPORT)
async def export_to_excel(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer('⏳ Генерирую отчёт...')

    try:
        client = MoySkladClient()
        payments = await client.fetch_payments(limit=_ALL_RECORDS)
    except Exception as e:
        logger.error(f'Failed to fetch payments for export: {e}')
        await callback.message.answer(
            '❌ Не удалось загрузить операции из МойСклад.',
            reply_markup=finance_menu_keyboard(),
        )
        return

    if not payments:
        await callback.message.answer('Нет операций для экспорта.', reply_markup=finance_menu_keyboard())
        return

    async with session_scope() as session:
        category_detail_map = await _build_category_detail_map(session)

    file_bytes, filename = generate_excel_from_ms(payments, category_detail_map)
    total = len(payments)

    await callback.message.answer_document(
        document=BufferedInputFile(file_bytes.read(), filename=filename),
        caption=f'📥 Экспорт завершён. Всего операций: {total}',
    )
    await callback.message.answer('💰 <b>Финансы</b>\n\nВыберите действие:', reply_markup=finance_menu_keyboard())
