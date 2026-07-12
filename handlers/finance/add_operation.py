"""FSM-хэндлер добавления финансовой операции"""

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from core import config
from core.database import OperationType, DocumentSubtype
from services.category_service import get_active_categories, get_category_by_id
from services.transaction_service import parse_amount, format_amount, type_label
from integrations.moysklad import MoySkladClient
from shared import session_scope
from shared.constants import CallbackData, CallbackPrefix
from shared.filters import IsAdminFilter
from shared.keyboards import (
    cancel_keyboard, categories_keyboard, comment_keyboard, confirm_keyboard,
    finance_menu_keyboard, operation_type_keyboard, operation_subtype_keyboard,
)
from shared.states import AddOperationState

_SUBTYPE_CALLBACKS = {
    CallbackData.SUBTYPE_PAYMENT_IN: DocumentSubtype.PAYMENT_IN,
    CallbackData.SUBTYPE_CASH_IN: DocumentSubtype.CASH_IN,
    CallbackData.SUBTYPE_PAYMENT_OUT: DocumentSubtype.PAYMENT_OUT,
    CallbackData.SUBTYPE_CASH_OUT: DocumentSubtype.CASH_OUT,
}

_SUBTYPE_LABELS = {
    DocumentSubtype.PAYMENT_IN.value: '🏦 Входящий платёж',
    DocumentSubtype.CASH_IN.value: '💵 Приходный ордер',
    DocumentSubtype.PAYMENT_OUT.value: '🏦 Исходящий платёж',
    DocumentSubtype.CASH_OUT.value: '💵 Расходный ордер',
}

router = Router()
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())



@router.callback_query(F.data == CallbackData.FINANCE_ADD)
async def start_add_operation(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(AddOperationState.waiting_type)
    msg = await callback.message.answer(
        '➕ <b>Новая операция</b>\n\nВыберите тип:',
        reply_markup=operation_type_keyboard(),
    )
    await state.update_data(last_bot_message_id=msg.message_id)


@router.callback_query(F.data.in_({CallbackData.TYPE_INCOME, CallbackData.TYPE_EXPENSE}),
                       AddOperationState.waiting_type)
async def process_type(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    op_type = OperationType.INCOME if callback.data == CallbackData.TYPE_INCOME else OperationType.EXPENSE
    await state.update_data(op_type=op_type.value)
    await state.set_state(AddOperationState.waiting_subtype)
    msg = await callback.message.answer(
        f'Тип: {type_label(op_type)}\n\nВыберите документ:',
        reply_markup=operation_subtype_keyboard(is_income=op_type == OperationType.INCOME),
    )
    await state.update_data(last_bot_message_id=msg.message_id)


@router.callback_query(F.data.in_(set(_SUBTYPE_CALLBACKS.keys())),
                       AddOperationState.waiting_subtype)
async def process_subtype(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    subtype = _SUBTYPE_CALLBACKS[callback.data]
    await state.update_data(doc_subtype=subtype.value)

    data = await state.get_data()
    op_type = OperationType(data['op_type'])

    async with session_scope() as session:
        categories = await get_active_categories(session, op_type)

    if not categories:
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        from shared.constants import ButtonLabel
        if op_type == OperationType.EXPENSE:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=ButtonLabel.SYNC_CATEGORIES, callback_data=CallbackData.CAT_SYNC)],
                [InlineKeyboardButton(text=ButtonLabel.CATEGORIES, callback_data=CallbackData.FINANCE_CATEGORIES)],
                [InlineKeyboardButton(text=ButtonLabel.BACK, callback_data=CallbackData.FINANCE_MENU)],
            ])
            text = '⚠️ Нет категорий расходов. Синхронизируйте с МойСклад или добавьте вручную.'
        else:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=ButtonLabel.CATEGORIES, callback_data=CallbackData.FINANCE_CATEGORIES)],
                [InlineKeyboardButton(text=ButtonLabel.BACK, callback_data=CallbackData.FINANCE_MENU)],
            ])
            text = '⚠️ Нет категорий доходов. Добавьте их в разделе «Категории».'
        await callback.message.answer(text, reply_markup=kb)
        await state.clear()
        return

    await state.set_state(AddOperationState.waiting_category)
    msg = await callback.message.answer(
        f'Тип: {type_label(op_type)} · {_SUBTYPE_LABELS[subtype.value]}\n\nВыберите категорию:',
        reply_markup=categories_keyboard(categories),
    )
    await state.update_data(last_bot_message_id=msg.message_id)


@router.callback_query(F.data.startswith(CallbackPrefix.SELECT_CATEGORY),
                       AddOperationState.waiting_category)
async def process_category(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    cat_id = int(callback.data[len(CallbackPrefix.SELECT_CATEGORY):])

    async with session_scope() as session:
        cat = await get_category_by_id(session, cat_id)

    if cat is None:
        await callback.message.answer('❌ Категория не найдена.', reply_markup=finance_menu_keyboard())
        await state.clear()
        return

    await state.update_data(category_id=cat_id, category_name=cat.name)
    await state.set_state(AddOperationState.waiting_amount)

    data = await state.get_data()
    msg = await callback.message.answer(
        f'Тип: {type_label(data["op_type"])}\n'
        f'Категория: {cat.name}\n\n'
        f'Введите сумму (например: <code>1500.50</code> или <code>3000</code>):',
        reply_markup=cancel_keyboard(),
    )
    await state.update_data(last_bot_message_id=msg.message_id)


@router.message(AddOperationState.waiting_amount)
async def process_amount(message: Message, state: FSMContext):
    amount_kopecks = parse_amount(message.text)
    if amount_kopecks is None:
        msg = await message.answer(
            '❌ Некорректная сумма. Введите положительное число, например <code>1500.50</code>:',
            reply_markup=cancel_keyboard(),
        )
        await state.update_data(last_bot_message_id=msg.message_id)
        return

    await state.update_data(amount_kopecks=amount_kopecks)
    await state.set_state(AddOperationState.waiting_comment)

    data = await state.get_data()
    msg = await message.answer(
        f'Тип: {type_label(data["op_type"])}\n'
        f'Категория: {data["category_name"]}\n'
        f'Сумма: {format_amount(amount_kopecks)}\n\n'
        f'Введите комментарий или нажмите «Без комментария»:',
        reply_markup=comment_keyboard(),
    )
    await state.update_data(last_bot_message_id=msg.message_id)


@router.callback_query(F.data == CallbackData.SKIP_COMMENT, AddOperationState.waiting_comment)
async def skip_comment(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(comment=None)
    await _show_confirm(callback.message, state)


@router.message(AddOperationState.waiting_comment)
async def process_comment(message: Message, state: FSMContext):
    author = config.FINANCE_COMMENT_AUTHOR
    prefix = f'{author}: ' if author else ''
    await state.update_data(comment=f'{prefix}{message.text.strip()}')
    await _show_confirm(message, state)


async def _show_confirm(message_or_msg, state: FSMContext):
    data = await state.get_data()
    comment_text = data.get('comment') or '—'
    subtype_label = _SUBTYPE_LABELS.get(data.get('doc_subtype', ''), '')
    text = (
        f'<b>Подтвердите операцию:</b>\n\n'
        f'Тип: {type_label(data["op_type"])} · {subtype_label}\n'
        f'Категория: {data["category_name"]}\n'
        f'Сумма: {format_amount(data["amount_kopecks"])}\n'
        f'Комментарий: {comment_text}'
    )
    await state.set_state(AddOperationState.waiting_confirm)
    msg = await message_or_msg.answer(text, reply_markup=confirm_keyboard())
    await state.update_data(last_bot_message_id=msg.message_id)


@router.callback_query(F.data == CallbackData.CONFIRM_SAVE, AddOperationState.waiting_confirm)
async def confirm_save(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()

    async with session_scope() as session:
        category = await get_category_by_id(session, data['category_id'])

    try:
        client = MoySkladClient()
        await client.create_payment(
            op_type=OperationType(data['op_type']),
            subtype=DocumentSubtype(data['doc_subtype']),
            amount_kopecks=data['amount_kopecks'],
            category=category,
            comment=data.get('comment'),
        )
    except Exception:
        await state.clear()
        await callback.message.answer('❌ Ошибка при сохранении операции.', reply_markup=finance_menu_keyboard())
        return

    await state.clear()
    await callback.message.answer(
        f'✅ <b>Операция сохранена!</b>\n\n'
        f'Тип: {type_label(data["op_type"])}\n'
        f'Категория: {data["category_name"]}\n'
        f'Сумма: {format_amount(data["amount_kopecks"])}',
        reply_markup=finance_menu_keyboard(),
    )


@router.callback_query(F.data == CallbackData.CONFIRM_CANCEL)
async def confirm_cancel(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.answer('❌ Операция отменена.', reply_markup=finance_menu_keyboard())
