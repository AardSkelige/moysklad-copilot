from decimal import Decimal

import pytest

from core import config
from services import stock_alerts
from services.stock_alerts import LowStock, find_low_stock, format_alert

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

MS = 'https://api.moysklad.ru/api/remap/1.2'


def product(pid, minimum):
    return {'id': pid, 'minimumBalance': minimum}


def row(pid, name, left, folder='Производство', path='', uom='шт', in_transit=0):
    return {'meta': {'href': f'{MS}/entity/product/{pid}?expand=supplier'}, 'name': name, 'stock': left,
            'inTransit': in_transit, 'uom': {'name': uom}, 'folder': {'name': folder, 'pathName': path}}


class FakeClient:
    def __init__(self, products, rows, fail=False):
        self.products, self.rows, self.fail = products, rows, fail
        self.asked, self.queries = 0, []

    async def stock_all(self, session, **modes):
        self.queries.append(modes)
        if self.fail:
            raise RuntimeError('503')
        return self.rows

    async def list_entities(self, session, entity, **kwargs):
        self.asked += 1
        return self.products


class Bot:
    def __init__(self, blocked=()):
        self.sent, self.blocked = [], set(blocked)

    async def send_message(self, chat_id, text):
        if chat_id in self.blocked:
            raise RuntimeError('Forbidden: bot was blocked by the user')
        self.sent.append((chat_id, text))


class _Session:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def session(monkeypatch):
    monkeypatch.setattr(stock_alerts, 'new_session', lambda: _Session())


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
        assert len(messages) > 1 and all(len(text) <= 4096 for text in messages)
        assert sum(text.count('• ') for text in messages) == 80


class TestSend:
    async def test_goes_to_listed_ids_even_without_bot_menu(self, monkeypatch, session):
        monkeypatch.setattr(config, 'STOCK_ALERT_RECIPIENT_IDS', [3, 4])
        bot = Bot()
        await stock_alerts.send_stock_alerts(bot, FakeClient([product('a', 10)], [row('a', 'Сода', 5.0)]))
        assert [chat for chat, _ in bot.sent] == [3, 4]

    async def test_nothing_low_sends_nothing(self, monkeypatch, session):
        monkeypatch.setattr(config, 'STOCK_ALERT_RECIPIENT_IDS', [3])
        bot = Bot()
        assert await stock_alerts.send_stock_alerts(bot, FakeClient([], [])) == 0 and bot.sent == []

    async def test_no_recipients_means_no_moysklad_calls(self, monkeypatch, session):
        monkeypatch.setattr(config, 'STOCK_ALERT_RECIPIENT_IDS', [])
        client = FakeClient([], [row('a', 'Сода', 5.0)])
        assert await stock_alerts.send_stock_alerts(Bot(), client) == 0 and client.queries == []

    async def test_moysklad_failure_warns_owner_not_recipients(self, monkeypatch, session):
        monkeypatch.setattr(config, 'STOCK_ALERT_RECIPIENT_IDS', [3])
        monkeypatch.setattr(config, 'AUDIT_OWNER_TELEGRAM_ID', 1)
        bot = Bot()
        assert await stock_alerts.send_stock_alerts(bot, FakeClient([], [], fail=True)) == 0
        [(chat, text)] = bot.sent
        assert chat == 1 and 'не ушла' in text


class TestOwnerWarnings:
    async def test_recipient_who_never_started_bot_is_reported_to_owner(self, monkeypatch, session):
        monkeypatch.setattr(config, 'STOCK_ALERT_RECIPIENT_IDS', [3])
        monkeypatch.setattr(config, 'AUDIT_OWNER_TELEGRAM_ID', 1)
        bot = Bot(blocked={3})
        await stock_alerts.send_stock_alerts(bot, FakeClient([product('a', 10)], [row('a', 'Сода', 5.0)]))
        [(chat, text)] = bot.sent
        assert chat == 1 and 'не дошла до 3' in text

    async def test_long_error_is_cut_before_escaping(self, monkeypatch, session):
        monkeypatch.setattr(config, 'STOCK_ALERT_RECIPIENT_IDS', [3])
        monkeypatch.setattr(config, 'AUDIT_OWNER_TELEGRAM_ID', 1)

        class Failing(FakeClient):
            async def stock_all(self, session, **modes):
                raise RuntimeError('"' * 300)

        bot = Bot()
        await stock_alerts.send_stock_alerts(bot, Failing([], []))
        [(_, text)] = bot.sent
        assert '&quot;' * 200 in text and not text.split('(')[1].rstrip(').').endswith('&quo')
