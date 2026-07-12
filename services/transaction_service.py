from typing import Optional

from core.database import OperationType


PAGE_SIZE = 10


def type_label(op_type: OperationType | str) -> str:
    """Иконка + название типа операции для отображения пользователю."""
    val = op_type.value if isinstance(op_type, OperationType) else op_type
    return '➕ Доход' if val == OperationType.INCOME.value else '➖ Расход'


def type_label_short(op_type: OperationType | str) -> str:
    """Только текст без иконки — для Excel и других экспортов."""
    val = op_type.value if isinstance(op_type, OperationType) else op_type
    return 'Доход' if val == OperationType.INCOME.value else 'Расход'


def parse_amount(text: str) -> Optional[int]:
    """Парсит сумму из строки. Возвращает kopecks или None при ошибке."""
    if text is None:
        return None
    text = text.strip().replace(',', '.').replace(' ', '')
    try:
        value = float(text)
    except ValueError:
        return None
    if value <= 0:
        return None
    return int(round(value * 100))


def format_amount(amount_kopecks: int) -> str:
    return f"{amount_kopecks / 100:,.2f} ₽".replace(',', ' ')
