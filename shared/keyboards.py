from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
)

from core import config
from shared.constants import CallbackData, CallbackPrefix, ButtonLabel

PRODUCTION_EXIT_TEXT = '❌ Выйти из режима производства'


def main_menu_keyboard(user_id: int | None = None) -> InlineKeyboardMarkup:
    rows = []
    if user_id is None or user_id in config.FINANCE_ADMIN_IDS:
        rows.append([InlineKeyboardButton(text=ButtonLabel.FINANCE, callback_data=CallbackData.FINANCE_MENU)])
    if config.AGENT_ENABLED and user_id in config.PRODUCTION_USERS:
        rows.append([InlineKeyboardButton(text=ButtonLabel.PRODUCTION, callback_data=CallbackData.PRODUCTION_ENTRY)])
    if config.AUDIT_ENABLED and user_id == config.AUDIT_OWNER_TELEGRAM_ID:
        rows.append([InlineKeyboardButton(text=ButtonLabel.AUDIT, callback_data=CallbackData.AUDIT_MENU)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def finance_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=ButtonLabel.ADD_OPERATION, callback_data=CallbackData.FINANCE_ADD)],
        [InlineKeyboardButton(text=ButtonLabel.VIEW_OPERATIONS, callback_data=CallbackData.FINANCE_LIST)],
        [InlineKeyboardButton(text=ButtonLabel.CATEGORIES, callback_data=CallbackData.FINANCE_CATEGORIES)],
        [InlineKeyboardButton(text=ButtonLabel.EXPORT, callback_data=CallbackData.FINANCE_EXPORT)],
        [InlineKeyboardButton(text=ButtonLabel.BACK, callback_data=CallbackData.MAIN_MENU)],
    ])


def audit_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=ButtonLabel.AUDIT_RUN, callback_data=CallbackData.AUDIT_RUN)],
        [InlineKeyboardButton(text=ButtonLabel.AUDIT_LIST, callback_data=CallbackData.AUDIT_LIST)],
        [InlineKeyboardButton(text=ButtonLabel.AUDIT_COMMENTS, callback_data=CallbackData.AUDIT_COMMENTS)],
        [InlineKeyboardButton(text=ButtonLabel.AUDIT_STATUS, callback_data=CallbackData.AUDIT_STATUS)],
        [InlineKeyboardButton(text=ButtonLabel.BACK, callback_data=CallbackData.MAIN_MENU)],
    ])


def audit_findings_keyboard() -> InlineKeyboardMarkup:
    """Кнопка разбора находок — сообщения бота ведут кнопкой, а не текстом команды."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=ButtonLabel.AUDIT_LIST, callback_data=CallbackData.AUDIT_LIST)],
        [InlineKeyboardButton(text=ButtonLabel.BACK, callback_data=CallbackData.AUDIT_MENU)],
    ])


def production_confirm_keyboard(action_label: str = '✅ Создать') -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=action_label, callback_data=CallbackData.PRODUCTION_CONFIRM),
            InlineKeyboardButton(text='❌ Отмена', callback_data=CallbackData.PRODUCTION_CANCEL),
        ],
    ])


def production_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='❌ Отмена', callback_data=CallbackData.PRODUCTION_CANCEL)],
    ])


def production_exit_reply_keyboard() -> ReplyKeyboardMarkup:
    """Постоянная reply-клавиатура снизу — единственный способ выйти из режима."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=PRODUCTION_EXIT_TEXT)]],
        resize_keyboard=True,
        is_persistent=True,
    )


def remove_reply_keyboard() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


def operation_type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=ButtonLabel.INCOME, callback_data=CallbackData.TYPE_INCOME),
            InlineKeyboardButton(text=ButtonLabel.EXPENSE, callback_data=CallbackData.TYPE_EXPENSE),
        ],
        [InlineKeyboardButton(text=ButtonLabel.CANCEL, callback_data=CallbackData.CONFIRM_CANCEL)],
    ])


def operation_subtype_keyboard(is_income: bool) -> InlineKeyboardMarkup:
    if is_income:
        rows = [
            [InlineKeyboardButton(text=ButtonLabel.PAYMENT_IN, callback_data=CallbackData.SUBTYPE_PAYMENT_IN)],
            [InlineKeyboardButton(text=ButtonLabel.CASH_IN, callback_data=CallbackData.SUBTYPE_CASH_IN)],
            [InlineKeyboardButton(text=ButtonLabel.CANCEL, callback_data=CallbackData.CONFIRM_CANCEL)],
        ]
    else:
        rows = [
            [InlineKeyboardButton(text=ButtonLabel.PAYMENT_OUT, callback_data=CallbackData.SUBTYPE_PAYMENT_OUT)],
            [InlineKeyboardButton(text=ButtonLabel.CASH_OUT, callback_data=CallbackData.SUBTYPE_CASH_OUT)],
            [InlineKeyboardButton(text=ButtonLabel.CANCEL, callback_data=CallbackData.CONFIRM_CANCEL)],
        ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def categories_keyboard(categories: list) -> InlineKeyboardMarkup:
    """2 кнопки в ряд"""
    rows = []
    row = []
    for cat in categories:
        btn = InlineKeyboardButton(
            text=cat.name,
            callback_data=f"{CallbackPrefix.SELECT_CATEGORY}{cat.id}"
        )
        row.append(btn)
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text=ButtonLabel.CANCEL, callback_data=CallbackData.CONFIRM_CANCEL)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=ButtonLabel.CANCEL, callback_data=CallbackData.CONFIRM_CANCEL)],
    ])


def comment_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=ButtonLabel.SKIP, callback_data=CallbackData.SKIP_COMMENT)],
        [InlineKeyboardButton(text=ButtonLabel.CANCEL, callback_data=CallbackData.CONFIRM_CANCEL)],
    ])


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=ButtonLabel.SAVE, callback_data=CallbackData.CONFIRM_SAVE),
            InlineKeyboardButton(text=ButtonLabel.CANCEL, callback_data=CallbackData.CONFIRM_CANCEL),
        ],
    ])


def categories_menu_keyboard() -> InlineKeyboardMarkup:
    """Главное меню управления категориями расходов"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=ButtonLabel.ADD_CATEGORY, callback_data=CallbackData.CAT_ADD)],
        [InlineKeyboardButton(text=ButtonLabel.RENAME_CATEGORY, callback_data=CallbackData.CAT_RENAME_LIST)],
        [InlineKeyboardButton(text=ButtonLabel.SYNC_CATEGORIES, callback_data=CallbackData.CAT_SYNC)],
        [InlineKeyboardButton(text=ButtonLabel.DELETE_CATEGORY_INFO, callback_data=CallbackData.CAT_DELETE_INFO)],
        [InlineKeyboardButton(text=ButtonLabel.BACK, callback_data=CallbackData.CAT_BACK)],
    ])


def categories_rename_select_keyboard(categories: list) -> InlineKeyboardMarkup:
    """Список категорий для выбора переименования, 2 в ряд"""
    rows = []
    row = []
    for cat in categories:
        btn = InlineKeyboardButton(
            text=cat.name,
            callback_data=f"{CallbackPrefix.RENAME_CATEGORY}{cat.id}"
        )
        row.append(btn)
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text=ButtonLabel.BACK, callback_data=CallbackData.FINANCE_CATEGORIES)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _ms_brief_amount(kopecks: int) -> str:
    rubles = kopecks / 100
    if rubles == int(rubles):
        return f"{int(rubles):,} ₽".replace(',', '\u202f')
    return f"{rubles:,.2f} ₽".replace(',', '\u202f')


def ms_operations_list_keyboard(payments: list, page: int, total_pages: int, category_map: dict = None) -> InlineKeyboardMarkup:
    """Список МС-операций: каждая операция — одна кнопка с кратким содержанием"""
    rows = []
    cat_map = category_map or {}
    for p in payments:
        icon = '➕' if p['op_type'].value == 'INCOME' else '➖'
        amount = _ms_brief_amount(p['amount_kopecks'])
        date = p['moment'].strftime('%d.%m')
        if p['op_type'].value == 'EXPENSE' and p['expense_item_href']:
            label_mid = cat_map.get(p['expense_item_href'], '?')
        elif p['comment']:
            label_mid = p['comment']
        else:
            label_mid = 'Доход'
        label = f"{icon} {amount} · {label_mid} · {date}"
        if len(label) > 60:
            label = label[:57] + '…'
        rows.append([InlineKeyboardButton(
            text=label,
            callback_data=f"{CallbackPrefix.MS_VIEW}{p['ms_id']}"
        )])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(
            text=ButtonLabel.PREV,
            callback_data=f"{CallbackPrefix.LIST_PAGE}{page - 1}"
        ))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(
            text=ButtonLabel.NEXT,
            callback_data=f"{CallbackPrefix.LIST_PAGE}{page + 1}"
        ))
    if nav_row:
        rows.append(nav_row)

    rows.append([InlineKeyboardButton(text=ButtonLabel.BACK, callback_data=CallbackData.FINANCE_MENU)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ms_edit_field_keyboard(ms_id: str, is_expense: bool) -> InlineKeyboardMarkup:
    """Выбор поля для редактирования МС-операции"""
    rows = [
        [
            InlineKeyboardButton(text='Сумма', callback_data=CallbackData.MS_EDIT_AMOUNT),
            InlineKeyboardButton(text='Комментарий', callback_data=CallbackData.MS_EDIT_COMMENT),
        ],
    ]
    if is_expense:
        rows.append([InlineKeyboardButton(text='Категория', callback_data=CallbackData.MS_EDIT_CATEGORY)])
    rows.append([InlineKeyboardButton(text=ButtonLabel.CANCEL, callback_data=CallbackData.MS_EDIT_CANCEL)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ms_delete_confirm_keyboard(ms_id: str) -> InlineKeyboardMarkup:
    """Подтверждение удаления МС-операции"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text='✅ Подтвердить',
                callback_data=f"{CallbackPrefix.MS_DELETE_CONFIRM}{ms_id}"
            ),
            InlineKeyboardButton(
                text=ButtonLabel.CANCEL,
                callback_data=CallbackData.MS_DELETE_CANCEL
            ),
        ],
    ])


def ms_operation_detail_keyboard(ms_id: str) -> InlineKeyboardMarkup:
    """Клавиатура детальной страницы МС-операции"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=ButtonLabel.EDIT, callback_data=f"{CallbackPrefix.MS_EDIT}{ms_id}"),
            InlineKeyboardButton(text=ButtonLabel.DELETE, callback_data=f"{CallbackPrefix.MS_DELETE}{ms_id}"),
        ],
        [InlineKeyboardButton(text='◀️ К списку', callback_data=CallbackData.MS_BACK_LIST)],
    ])


def ms_edit_categories_keyboard(categories: list) -> InlineKeyboardMarkup:
    """Выбор категории при редактировании МС-операции, 2 в ряд"""
    rows = []
    row = []
    for cat in categories:
        btn = InlineKeyboardButton(
            text=cat.name,
            callback_data=f"{CallbackPrefix.MS_EDIT_CAT}{cat.id}"
        )
        row.append(btn)
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text=ButtonLabel.CANCEL, callback_data=CallbackData.MS_EDIT_CANCEL)])
    return InlineKeyboardMarkup(inline_keyboard=rows)
