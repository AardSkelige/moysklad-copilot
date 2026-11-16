import pytest

from core import config
from services import stock_alerts
from tests.unit.stock_fakes import Bot, FakeClient, FakeSession, product, row

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


@pytest.fixture
def session(monkeypatch):
    monkeypatch.setattr(stock_alerts, 'new_session', lambda: FakeSession())


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
