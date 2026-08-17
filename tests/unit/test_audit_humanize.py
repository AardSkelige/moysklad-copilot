"""Перевод копеек в рубли перед отправкой фактов в LLM.

Живой кейс: приёмка №00065 по Лауроилглутамату — цена 45 копеек (0,45 ₽ за грамм).
LLM прочитала её как «45,00 ₽/кг» и построила на этом весь вердикт.
"""

import pytest

from services.audit.humanize import format_rub, humanize_money

pytestmark = [pytest.mark.unit, pytest.mark.audit]


class TestFormatRub:
    def test_kopecks_to_rubles(self):
        assert format_rub(45) == '0,45 ₽'
        assert format_rub(170) == '1,70 ₽'

    def test_thousands_separated_by_space(self):
        assert format_rub(1125000) == '11 250,00 ₽'

    def test_fractional_kopecks_rounded(self):
        assert format_rub(80.7866311051842) == '0,81 ₽'

    def test_none_is_dash(self):
        assert format_rub(None) == '—'


class TestHumanizeMoney:
    def test_kopecks_key_replaced_by_ruble_string(self):
        out = humanize_money({'price_kopecks': 45})
        assert out == {'price': '0,45 ₽'}

    def test_non_money_keys_untouched(self):
        facts = {'stock': 5999.0, 'uom': 'г', 'deviation_percent': 52.5}
        assert humanize_money(facts) == facts

    def test_nested_dicts_and_lists(self):
        facts = {
            'fifo_kopecks': 80.7866311051842,
            'last_supply': {'price_kopecks': 170, 'supply': '00079'},
            'source_documents': [
                {'doc': 'Приёмка №00065', 'price_kopecks': 45, 'quantity': 5000},
                {'doc': 'Приёмка №00079', 'price_kopecks': 170, 'quantity': 1000},
            ],
        }
        out = humanize_money(facts)
        assert out['fifo'] == '0,81 ₽'
        assert out['last_supply'] == {'price': '1,70 ₽', 'supply': '00079'}
        assert [d['price'] for d in out['source_documents']] == ['0,45 ₽', '1,70 ₽']
        assert out['source_documents'][0]['quantity'] == 5000

    def test_keep_raw_leaves_kopecks_for_fix_actions(self):
        out = humanize_money({'price_kopecks': 45}, keep_raw=True)
        assert out == {'price': '0,45 ₽', 'price_kopecks': 45}

    def test_existing_plain_key_not_overwritten(self):
        out = humanize_money({'price': 'договорная', 'price_kopecks': 45})
        assert out['price'] == 'договорная'
        assert out['price_rub'] == '0,45 ₽'

    def test_source_not_mutated(self):
        facts = {'price_kopecks': 45}
        humanize_money(facts)
        assert facts == {'price_kopecks': 45}
