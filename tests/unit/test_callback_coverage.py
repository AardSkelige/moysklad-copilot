"""
Проверка покрытия callback_data.

Каждая кнопка во всех клавиатурах должна использовать только
константы из CallbackData или префиксы из CallbackPrefix.
Если кто-то напишет кнопку с произвольной строкой — тест упадёт.
"""

import pytest
from unittest.mock import MagicMock

from shared.constants import CallbackData, CallbackPrefix
from shared.keyboards import (
    main_menu_keyboard,
    operation_type_keyboard,
    categories_keyboard,
    comment_keyboard,
    confirm_keyboard,
    categories_menu_keyboard,
    categories_rename_select_keyboard,
)

pytestmark = [pytest.mark.unit]

# Все допустимые точные значения callback_data
KNOWN_EXACT = {
    v for k, v in vars(CallbackData).items()
    if not k.startswith('_') and isinstance(v, str)
}

# Все допустимые префиксы
KNOWN_PREFIXES = tuple(
    v for k, v in vars(CallbackPrefix).items()
    if not k.startswith('_') and isinstance(v, str)
)


def _make_cat(cat_id: int = 1, name: str = 'Тест') -> MagicMock:
    cat = MagicMock()
    cat.id = cat_id
    cat.name = name
    return cat


def _extract_callbacks(keyboard) -> list[str]:
    """Извлекает все callback_data из InlineKeyboardMarkup."""
    result = []
    for row in keyboard.inline_keyboard:
        for btn in row:
            if btn.callback_data:
                result.append(btn.callback_data)
    return result


def _assert_all_known(callbacks: list[str], keyboard_name: str):
    for cb in callbacks:
        is_exact = cb in KNOWN_EXACT
        is_prefixed = cb.startswith(KNOWN_PREFIXES)
        assert is_exact or is_prefixed, (
            f"\n[{keyboard_name}] Неизвестный callback_data: '{cb}'\n"
            f"Добавьте константу в CallbackData или CallbackPrefix."
        )


class TestCallbackCoverage:
    """Все кнопки используют только объявленные константы."""

    pytestmark = [pytest.mark.unit]

    def test_main_menu_keyboard(self):
        kb = main_menu_keyboard()
        _assert_all_known(_extract_callbacks(kb), 'main_menu_keyboard')

    def test_operation_type_keyboard(self):
        kb = operation_type_keyboard()
        _assert_all_known(_extract_callbacks(kb), 'operation_type_keyboard')

    def test_categories_keyboard_empty(self):
        kb = categories_keyboard([])
        _assert_all_known(_extract_callbacks(kb), 'categories_keyboard(empty)')

    def test_categories_keyboard_with_items(self):
        cats = [_make_cat(1), _make_cat(2)]
        kb = categories_keyboard(cats)
        _assert_all_known(_extract_callbacks(kb), 'categories_keyboard')

    def test_comment_keyboard(self):
        kb = comment_keyboard()
        _assert_all_known(_extract_callbacks(kb), 'comment_keyboard')

    def test_confirm_keyboard(self):
        kb = confirm_keyboard()
        _assert_all_known(_extract_callbacks(kb), 'confirm_keyboard')

    def test_categories_menu_keyboard(self):
        kb = categories_menu_keyboard()
        _assert_all_known(_extract_callbacks(kb), 'categories_menu_keyboard')

    def test_categories_rename_select_keyboard_empty(self):
        kb = categories_rename_select_keyboard([])
        _assert_all_known(_extract_callbacks(kb), 'categories_rename_select_keyboard(empty)')

    def test_categories_rename_select_keyboard_with_items(self):
        cats = [_make_cat(1), _make_cat(2)]
        kb = categories_rename_select_keyboard(cats)
        _assert_all_known(_extract_callbacks(kb), 'categories_rename_select_keyboard')
