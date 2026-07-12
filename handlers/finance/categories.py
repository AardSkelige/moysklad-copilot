"""Управление категориями расходов"""

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from sqlalchemy import select, func

from core.database import ApplicableTo, Category
from core.logger import logger
from integrations.moysklad import MoySkladClient
from services.category_service import (
    get_active_categories, create_category, get_category_by_id, rename_category,
)
from services.moysklad_service import sync_expense_categories_from_moysklad
from shared import session_scope
from shared.constants import CallbackData, CallbackPrefix
from shared.filters import IsAdminFilter
from shared.keyboards import (
    cancel_keyboard, categories_menu_keyboard, categories_rename_select_keyboard, finance_menu_keyboard,
)
from shared.states import AddCategoryState, RenameCategoryState

router = Router()
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())


@router.callback_query(F.data == CallbackData.FINANCE_CATEGORIES)
async def show_categories(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.answer(
        '📁 <b>Управление категориями расходов</b>\n\nВыберите действие:',
        reply_markup=categories_menu_keyboard(),
    )


@router.callback_query(F.data == CallbackData.CAT_BACK)
async def categories_back(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer('💰 <b>Финансы</b>\n\nВыберите действие:', reply_markup=finance_menu_keyboard())


# ── Добавить категорию ────────────────────────────────────────────────────────

@router.callback_query(F.data == CallbackData.CAT_ADD)
async def start_add_category(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(AddCategoryState.waiting_name)
    msg = await callback.message.answer(
        '✏️ Введите название новой категории расходов.\n'
        'Она будет создана в МойСклад как статья расходов.',
        reply_markup=cancel_keyboard(),
    )
    await state.update_data(last_bot_message_id=msg.message_id)


@router.message(AddCategoryState.waiting_name)
async def process_category_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if len(name) < 2:
        await message.answer('Название слишком короткое. Введите ещё раз:', reply_markup=cancel_keyboard())
        return

    try:
        client = MoySkladClient()
        href = await client.create_expense_item(name)
    except Exception as e:
        logger.error(f'Failed to create expense item in MoySklad: {e}')
        await message.answer(
            '❌ Не удалось создать категорию в МойСклад. Попробуйте позже.',
            reply_markup=finance_menu_keyboard(),
        )
        await state.clear()
        return

    async with session_scope() as session:
        cat = await create_category(
            session,
            name=name,
            applicable_to=ApplicableTo.EXPENSE,
            moysklad_expense_item_href=href,
        )

    await state.clear()
    await message.answer(
        f'✅ Категория «{cat.name}» создана и добавлена в МойСклад.',
        reply_markup=finance_menu_keyboard(),
    )


# ── Переименовать категорию ───────────────────────────────────────────────────

@router.callback_query(F.data == CallbackData.CAT_RENAME_LIST)
async def show_rename_list(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    async with session_scope() as session:
        categories = await get_active_categories(session)
    # Показываем только EXPENSE-категории (у которых есть href или нет — всё равно)
    expense_cats = [
        c for c in categories
        if c.applicable_to in (ApplicableTo.EXPENSE, ApplicableTo.BOTH) and not c.is_system
    ]

    if not expense_cats:
        await callback.message.answer(
            '📁 Нет категорий расходов для переименования.',
            reply_markup=categories_menu_keyboard(),
        )
        return

    await state.set_state(RenameCategoryState.waiting_select)
    await callback.message.answer(
        '✏️ <b>Переименовать категорию</b>\n\nВыберите категорию:',
        reply_markup=categories_rename_select_keyboard(expense_cats),
    )


@router.callback_query(
    F.data.startswith(CallbackPrefix.RENAME_CATEGORY),
    RenameCategoryState.waiting_select,
)
async def rename_category_selected(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    cat_id = int(callback.data[len(CallbackPrefix.RENAME_CATEGORY):])
    await state.update_data(cat_id=cat_id)
    await state.set_state(RenameCategoryState.waiting_new_name)
    await callback.message.answer('Введите новое название категории:', reply_markup=cancel_keyboard())


@router.message(RenameCategoryState.waiting_new_name)
async def process_rename_new_name(message: Message, state: FSMContext):
    new_name = message.text.strip()
    if len(new_name) < 2:
        await message.answer('Название слишком короткое. Введите ещё раз:', reply_markup=cancel_keyboard())
        return

    data = await state.get_data()
    cat_id = data['cat_id']

    async with session_scope() as session:
        cat = await get_category_by_id(session, cat_id)

        if not cat:
            await message.answer('❌ Категория не найдена.', reply_markup=finance_menu_keyboard())
            await state.clear()
            return

        # Сначала обновляем в МойСклад (если есть href)
        if cat.moysklad_expense_item_href:
            try:
                client = MoySkladClient()
                await client.update_expense_item(cat.moysklad_expense_item_href, new_name)
            except Exception as e:
                logger.error(f'Failed to rename expense item in MoySklad: {e}')
                await message.answer(
                    '❌ Не удалось переименовать в МойСклад. Попробуйте позже.',
                    reply_markup=finance_menu_keyboard(),
                )
                await state.clear()
                return

        await rename_category(session, cat_id, new_name)

    await state.clear()
    await message.answer(
        f'✅ Категория переименована в «{new_name}».',
        reply_markup=finance_menu_keyboard(),
    )


# ── Синхронизация с МойСклад ──────────────────────────────────────────────────

@router.callback_query(F.data == CallbackData.CAT_SYNC)
async def sync_categories(callback: CallbackQuery, state: FSMContext):
    await callback.answer()

    try:
        async with session_scope() as session:
            stats = await sync_expense_categories_from_moysklad(session)
            count_result = await session.execute(
                select(func.count()).where(
                    Category.is_active == True,
                    Category.applicable_to.in_([ApplicableTo.EXPENSE, ApplicableTo.BOTH]),
                    Category.moysklad_expense_item_href.isnot(None),
                )
            )
            local_count = count_result.scalar()

        await callback.message.answer(
            f'🔄 Синхронизация выполнена:\n'
            f'  • создано: {stats["created"]}\n'
            f'  • обновлено: {stats["updated"]}\n'
            f'  • удалено: {stats["deleted"]}\n\n'
            f'📊 Категорий сейчас:\n'
            f'  • в боте: {local_count}\n'
            f'  • в МойСклад: {stats["ms_count"]}',
            reply_markup=categories_menu_keyboard(),
        )
    except Exception as e:
        logger.error(f'Failed to sync expense categories: {e}')
        await callback.message.answer(
            '❌ Ошибка при синхронизации с МойСклад.',
            reply_markup=categories_menu_keyboard(),
        )


# ── Как удалить категорию ─────────────────────────────────────────────────────

@router.callback_query(F.data == CallbackData.CAT_DELETE_INFO)
async def show_delete_info(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer(
        'ℹ️ <b>Как удалить категорию расходов</b>\n\n'
        'Категории удаляются напрямую в МойСклад (раздел «Статьи расходов»).\n\n'
        'Если к категории привязаны операции — МойСклад не позволит удалить.\n\n'
        'После удаления в МС нажмите «🔄 Синхронизировать с МойСклад» — '
        'категория исчезнет из бота.',
        reply_markup=categories_menu_keyboard(),
    )
