"""Просмотр, редактирование, удаление операций — MoySklad-first"""

import math

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from core.database import OperationType
from core.logger import logger
from integrations.moysklad import MoySkladClient
from services.category_service import get_active_categories, get_category_by_id
from services.transaction_service import format_amount, type_label, parse_amount, PAGE_SIZE
from shared import session_scope
from shared.constants import CallbackData, CallbackPrefix
from shared.filters import IsAdminFilter
from shared.keyboards import (
    cancel_keyboard, finance_menu_keyboard,
    ms_operations_list_keyboard, ms_edit_field_keyboard, ms_delete_confirm_keyboard,
    ms_edit_categories_keyboard, ms_operation_detail_keyboard,
)
from shared.states import EditMSOperationState

router = Router()
router.callback_query.filter(IsAdminFilter())
router.message.filter(IsAdminFilter())


# ── Форматирование ─────────────────────────────────────────────────────────────

def _format_ms_list_text(page: int, total: int) -> str:
    total_pages = math.ceil(total / PAGE_SIZE) if total > 0 else 1
    if total == 0:
        return '📋 <b>Операции</b>\n\nЗаписей пока нет.'
    return f'📋 <b>Операции</b> (стр. {page + 1}/{total_pages}, всего {total})\nВыберите операцию:'


def _format_ms_detail_text(payment: dict, category_map: dict) -> str:
    type_icon = type_label(payment['op_type'])
    is_expense = payment['op_type'] == OperationType.EXPENSE
    if is_expense and payment['expense_item_href']:
        cat_name = category_map.get(payment['expense_item_href'], '?')
    else:
        cat_name = None
    date_str = payment['moment'].strftime('%d.%m.%Y %H:%M')

    lines = [
        '📄 <b>Операция</b>\n',
        f'Тип: {type_icon}',
        f'Сумма: {format_amount(payment["amount_kopecks"])}',
        f'Дата: {date_str}',
    ]
    if cat_name:
        lines.append(f'Категория: {cat_name}')
    if payment['comment']:
        lines.append(f'Назначение: {payment["comment"]}')
    return '\n'.join(lines)


# ── MoySklad рендер ────────────────────────────────────────────────────────────

async def _build_category_map(session) -> dict:
    """Map expense_item_href -> category.name из локальной БД"""
    from sqlalchemy import select
    from core.database import Category
    result = await session.execute(select(Category))
    cats = result.scalars().all()
    return {cat.moysklad_expense_item_href: cat.name for cat in cats if cat.moysklad_expense_item_href}


async def _render_ms_list(callback: CallbackQuery, state: FSMContext, page: int):
    """Загрузить из МС, сохранить в FSM, показать страницу (новое сообщение)"""
    try:
        client = MoySkladClient()
        payments = await client.fetch_payments()
    except Exception as e:
        logger.error(f'Failed to fetch payments from MoySklad: {e}')
        await callback.message.answer(
            '❌ Не удалось загрузить операции из МойСклад.',
            reply_markup=finance_menu_keyboard(),
        )
        return

    async with session_scope() as session:
        category_map = await _build_category_map(session)

    await state.update_data(ms_payments=payments, ms_category_map=category_map, ms_current_page=page)
    text = _format_ms_list_text(page, len(payments))
    total_pages = math.ceil(len(payments) / PAGE_SIZE) if payments else 1
    start = page * PAGE_SIZE
    page_payments = payments[start:start + PAGE_SIZE]
    keyboard = ms_operations_list_keyboard(page_payments, page, total_pages, category_map)
    await callback.message.answer(text, reply_markup=keyboard)


async def _render_ms_page_edit(callback: CallbackQuery, state: FSMContext, payments: list, category_map: dict, page: int):
    """Показать страницу списка, редактируя существующее сообщение"""
    total = len(payments)
    total_pages = math.ceil(total / PAGE_SIZE) if total > 0 else 1
    start = page * PAGE_SIZE
    page_payments = payments[start:start + PAGE_SIZE]
    text = _format_ms_list_text(page, total)
    keyboard = ms_operations_list_keyboard(page_payments, page, total_pages, category_map)
    await state.update_data(ms_current_page=page)
    await callback.message.edit_text(text, reply_markup=keyboard)


# ── Главные хэндлеры ──────────────────────────────────────────────────────────

@router.callback_query(F.data == CallbackData.FINANCE_LIST)
async def show_operations_list(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await _render_ms_list(callback, state, page=0)


@router.callback_query(F.data.startswith(CallbackPrefix.LIST_PAGE))
async def paginate_operations(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    page = int(callback.data[len(CallbackPrefix.LIST_PAGE):])

    data = await state.get_data()
    payments = data.get('ms_payments')
    category_map = data.get('ms_category_map', {})
    if payments is None:
        await _render_ms_list(callback, state, page=page)
    else:
        await _render_ms_page_edit(callback, state, payments, category_map, page)


# ── Детальный просмотр операции ───────────────────────────────────────────────

@router.callback_query(F.data.startswith(CallbackPrefix.MS_VIEW))
async def ms_view_detail(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    ms_id = callback.data[len(CallbackPrefix.MS_VIEW):]

    data = await state.get_data()
    payments = data.get('ms_payments', [])
    category_map = data.get('ms_category_map', {})

    payment = next((p for p in payments if p['ms_id'] == ms_id), None)
    if payment is None:
        await callback.message.answer('❌ Операция не найдена.', reply_markup=finance_menu_keyboard())
        return

    text = _format_ms_detail_text(payment, category_map)
    await callback.message.edit_text(text, reply_markup=ms_operation_detail_keyboard(ms_id))


@router.callback_query(F.data == CallbackData.MS_BACK_LIST)
async def ms_back_to_list(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    payments = data.get('ms_payments')
    category_map = data.get('ms_category_map', {})
    page = data.get('ms_current_page', 0)

    if payments is None:
        await _render_ms_list(callback, state, page=0)
    else:
        await _render_ms_page_edit(callback, state, payments, category_map, page)


# ── MoySklad редактирование ────────────────────────────────────────────────────

@router.callback_query(F.data.startswith(CallbackPrefix.MS_EDIT))
async def start_ms_edit(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    ms_id = callback.data[len(CallbackPrefix.MS_EDIT):]

    data = await state.get_data()
    payments = data.get('ms_payments', [])
    category_map = data.get('ms_category_map', {})

    payment = next((p for p in payments if p['ms_id'] == ms_id), None)
    if payment is None:
        await callback.message.answer('❌ Операция не найдена.', reply_markup=finance_menu_keyboard())
        return

    type_icon = type_label(payment['op_type'])
    is_expense = payment['op_type'] == OperationType.EXPENSE
    if is_expense and payment['expense_item_href']:
        cat_name = category_map.get(payment['expense_item_href'], '?')
    else:
        cat_name = '—'
    date_str = payment['moment'].strftime('%d.%m.%Y %H:%M')

    text = (
        f'✏️ <b>Редактирование</b>\n\n'
        f'Тип: {type_icon}\n'
        f'Сумма: {format_amount(payment["amount_kopecks"])}\n'
        f'Дата: {date_str}\n'
    )
    if is_expense:
        text += f'Категория: {cat_name}\n'
    text += f'Комментарий: {payment["comment"] or "—"}\n\nЧто изменить?'

    await state.set_state(EditMSOperationState.waiting_field)
    await state.update_data(
        ms_edit_id=ms_id,
        ms_edit_href=payment['href'],
        ms_edit_op_type=payment['op_type'].value,
    )
    await callback.message.edit_text(text, reply_markup=ms_edit_field_keyboard(ms_id, is_expense))


@router.callback_query(F.data == CallbackData.MS_EDIT_AMOUNT, EditMSOperationState.waiting_field)
async def ms_edit_select_amount(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(EditMSOperationState.waiting_amount)
    await callback.message.answer('Введите новую сумму (в рублях):', reply_markup=cancel_keyboard())


@router.callback_query(F.data == CallbackData.MS_EDIT_COMMENT, EditMSOperationState.waiting_field)
async def ms_edit_select_comment(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(EditMSOperationState.waiting_comment)
    await callback.message.answer('Введите новый комментарий:', reply_markup=cancel_keyboard())


@router.callback_query(F.data == CallbackData.MS_EDIT_CATEGORY, EditMSOperationState.waiting_field)
async def ms_edit_select_category(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    async with session_scope() as session:
        categories = await get_active_categories(session, OperationType.EXPENSE)

    await state.set_state(EditMSOperationState.waiting_category)
    await callback.message.answer(
        'Выберите новую категорию:',
        reply_markup=ms_edit_categories_keyboard(categories),
    )


@router.message(EditMSOperationState.waiting_amount)
async def ms_save_amount(message: Message, state: FSMContext):
    amount_kopecks = parse_amount(message.text)
    if amount_kopecks is None:
        await message.answer('❌ Некорректная сумма. Введите положительное число:', reply_markup=cancel_keyboard())
        return

    data = await state.get_data()
    href = data['ms_edit_href']
    ms_id = data['ms_edit_id']

    try:
        client = MoySkladClient()
        await client.update_payment_fields(href, {'sum': amount_kopecks})
    except Exception as e:
        logger.error(f'Failed to update payment amount in MoySklad: {e}')
        await message.answer('❌ Не удалось обновить сумму в МойСклад.', reply_markup=finance_menu_keyboard())
        await state.clear()
        return

    payments = data.get('ms_payments', [])
    for p in payments:
        if p['ms_id'] == ms_id:
            p['amount_kopecks'] = amount_kopecks
            break
    await state.update_data(ms_payments=payments)
    await state.clear()
    await message.answer(f'✅ Сумма изменена: {format_amount(amount_kopecks)}', reply_markup=finance_menu_keyboard())


@router.message(EditMSOperationState.waiting_comment)
async def ms_save_comment(message: Message, state: FSMContext):
    comment = message.text.strip()
    data = await state.get_data()
    href = data['ms_edit_href']
    ms_id = data['ms_edit_id']

    try:
        client = MoySkladClient()
        await client.update_payment_fields(href, {'paymentPurpose': comment})
    except Exception as e:
        logger.error(f'Failed to update payment comment in MoySklad: {e}')
        await message.answer('❌ Не удалось обновить комментарий в МойСклад.', reply_markup=finance_menu_keyboard())
        await state.clear()
        return

    payments = data.get('ms_payments', [])
    for p in payments:
        if p['ms_id'] == ms_id:
            p['comment'] = comment
            break
    await state.update_data(ms_payments=payments)
    await state.clear()
    await message.answer('✅ Комментарий изменён.', reply_markup=finance_menu_keyboard())


@router.callback_query(F.data.startswith(CallbackPrefix.MS_EDIT_CAT), EditMSOperationState.waiting_category)
async def ms_save_category(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    cat_id = int(callback.data[len(CallbackPrefix.MS_EDIT_CAT):])
    data = await state.get_data()
    href = data['ms_edit_href']
    ms_id = data['ms_edit_id']

    async with session_scope() as session:
        cat = await get_category_by_id(session, cat_id)

    if not cat or not cat.moysklad_expense_item_href:
        await callback.message.answer(
            '❌ Категория не найдена или не привязана к МойСклад.',
            reply_markup=finance_menu_keyboard(),
        )
        await state.clear()
        return

    try:
        client = MoySkladClient()
        await client.update_payment_fields(href, {
            'expenseItem': {
                'meta': {
                    'href': cat.moysklad_expense_item_href,
                    'type': 'expenseitem',
                    'mediaType': 'application/json',
                }
            }
        })
    except Exception as e:
        logger.error(f'Failed to update payment category in MoySklad: {e}')
        await callback.message.answer('❌ Не удалось обновить категорию в МойСклад.', reply_markup=finance_menu_keyboard())
        await state.clear()
        return

    payments = data.get('ms_payments', [])
    for p in payments:
        if p['ms_id'] == ms_id:
            p['expense_item_href'] = cat.moysklad_expense_item_href
            break
    await state.update_data(ms_payments=payments)
    await state.clear()
    await callback.message.answer(f'✅ Категория изменена: {cat.name}', reply_markup=finance_menu_keyboard())


@router.callback_query(F.data == CallbackData.MS_EDIT_CANCEL)
async def ms_edit_cancel(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.answer('Редактирование отменено.', reply_markup=finance_menu_keyboard())


# ── MoySklad удаление ─────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith(CallbackPrefix.MS_DELETE))
async def ms_delete_operation_confirm(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    ms_id = callback.data[len(CallbackPrefix.MS_DELETE):]

    data = await state.get_data()
    payments = data.get('ms_payments', [])
    category_map = data.get('ms_category_map', {})
    payment = next((p for p in payments if p['ms_id'] == ms_id), None)

    if payment:
        type_icon = type_label(payment['op_type'])
        amount = format_amount(payment['amount_kopecks'])
        date_str = payment['moment'].strftime('%d.%m.%Y')
        is_expense = payment['op_type'] == OperationType.EXPENSE
        if is_expense and payment['expense_item_href']:
            cat = category_map.get(payment['expense_item_href'], '?')
            detail = f'{type_icon} {amount} · {cat} · {date_str}'
        else:
            detail = f'{type_icon} {amount} · {date_str}'
        text = f'🗑 Удалить операцию?\n\n{detail}\n\nЭто действие необратимо.'
    else:
        text = '🗑 Удалить операцию? Это действие необратимо.'

    await callback.message.edit_text(text, reply_markup=ms_delete_confirm_keyboard(ms_id))


@router.callback_query(F.data.startswith(CallbackPrefix.MS_DELETE_CONFIRM))
async def ms_delete_execute(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    ms_id = callback.data[len(CallbackPrefix.MS_DELETE_CONFIRM):]

    data = await state.get_data()
    payments = data.get('ms_payments', [])

    payment = next((p for p in payments if p['ms_id'] == ms_id), None)
    if payment is None:
        await callback.message.answer('❌ Операция не найдена.', reply_markup=finance_menu_keyboard())
        return

    try:
        client = MoySkladClient()
        await client.delete_payment_by_href(payment['href'])
    except Exception as e:
        logger.error(f'Failed to delete payment from MoySklad: {e}')
        await callback.message.answer('❌ Не удалось удалить операцию из МойСклад.', reply_markup=finance_menu_keyboard())
        return

    payments = [p for p in payments if p['ms_id'] != ms_id]
    await state.update_data(ms_payments=payments)
    await callback.message.edit_text('✅ Операция удалена.', reply_markup=None)
    await callback.message.answer('Что дальше?', reply_markup=finance_menu_keyboard())


@router.callback_query(F.data == CallbackData.MS_DELETE_CANCEL)
async def ms_delete_cancel(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    payments = data.get('ms_payments', [])
    category_map = data.get('ms_category_map', {})
    page = data.get('ms_current_page', 0)
    if payments is not None:
        await _render_ms_page_edit(callback, state, payments, category_map, page)
    else:
        await callback.message.answer('Удаление отменено.', reply_markup=finance_menu_keyboard())
