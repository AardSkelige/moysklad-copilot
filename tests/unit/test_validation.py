"""Тесты валидации входных данных"""

import pytest

from services.transaction_service import parse_amount

pytestmark = [pytest.mark.unit, pytest.mark.validation]


class TestParseAmount:
    """Тесты парсинга суммы"""

    def test_valid_integer(self):
        assert parse_amount('1500') == 150000

    def test_valid_float(self):
        assert parse_amount('1500.50') == 150050

    def test_valid_with_comma(self):
        assert parse_amount('1500,50') == 150050

    def test_invalid_text(self):
        assert parse_amount('abc') is None

    def test_negative(self):
        assert parse_amount('-100') is None

    def test_zero(self):
        assert parse_amount('0') is None

    def test_none_input(self):
        assert parse_amount(None) is None

    def test_empty_string(self):
        assert parse_amount('') is None

    def test_with_spaces(self):
        assert parse_amount('1 500') == 150000
