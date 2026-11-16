from decimal import Decimal

import pytest

from services.low_stock import MESSAGE_LIMIT, LowStock, find_low_stock, format_alert
from tests.unit.stock_fakes import FakeClient, product, row

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


class TestFindLowStock:
    async def test_asks_moysklad_for_items_under_minimum(self):
        client = FakeClient([product('a', 700)], [row('a', 'Этикетка воск логотип', 624.0)])
        assert await find_low_stock(client, None) == [
            LowStock('Этикетка воск логотип', Decimal('624.0'), Decimal('700'), Decimal('0'), 'шт')]
        assert client.queries == [{'stock_mode': 'underMinimum', 'quantity_mode': 'all'}]

    async def test_finished_goods_are_left_to_insights(self):
        client = FakeClient([], [row('g', 'Шампунь 500 мл', 1.0, folder='Шампуни', path='Готовая продукция'),
                                 row('h', 'Воск 150 г', 1.0, folder='Готовая продукция')])
        assert await find_low_stock(client, None) == [] and client.asked == 0

    async def test_folder_only_named_like_finished_goods_is_kept(self):
        client = FakeClient([product('m', 3)], [row('m', 'Сырьё MIMO', 1.0, folder='Готовая продукция MIMO')])
        assert [low.name for low in await find_low_stock(client, None)] == ['Сырьё MIMO']

    async def test_minimum_set_per_warehouse_is_shown_without_threshold(self):
        client = FakeClient([product('a', 0)], [row('a', 'Сода', 1.0, uom='кг')])
        [low] = await find_low_stock(client, None)
        assert low.minimum is None


class TestMessage:
    async def test_lists_left_minimum_and_what_is_coming(self):
        [text] = format_alert([LowStock('Отдушка Черри', Decimal('120.5'), Decimal('500'), Decimal('1000'), 'г')])
        assert 'Ниже неснижаемого остатка: 1' in text
        assert '• Отдушка Черри — осталось 120,5 г, порог 500, ожидается по заказам поставщикам 1 000' in text

    async def test_names_are_escaped_for_html(self):
        [text] = format_alert([LowStock('Масло ши & какао <премиум>', Decimal('1'), None, Decimal('0'), 'г')])
        assert 'Масло ши &amp; какао &lt;премиум&gt; — осталось 1 г' in text and 'порог' not in text

    async def test_long_list_is_split_under_telegram_limit(self):
        items = [LowStock('Очень длинное название материала ' * 3 + str(i), Decimal('1'), Decimal('2'), Decimal('0'), 'шт')
                 for i in range(80)]
        messages = format_alert(items)
        assert len(messages) > 1 and all(len(text) <= MESSAGE_LIMIT for text in messages)
        assert sum(text.count('• ') for text in messages) == 80
