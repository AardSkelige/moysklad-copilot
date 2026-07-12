"""Тесты контроля доступа — IsAdminFilter"""

import pytest
from unittest.mock import MagicMock

from core import config
from shared.filters import IsAdminFilter

pytestmark = [pytest.mark.asyncio, pytest.mark.unit, pytest.mark.access]


def make_event(user_id: int) -> MagicMock:
    event = MagicMock()
    event.from_user = MagicMock()
    event.from_user.id = user_id
    return event


class TestIsAdminFilter:
    """Проверка фильтра доступа"""

    pytestmark = [pytest.mark.asyncio, pytest.mark.unit, pytest.mark.access]

    async def test_admin_passes(self):
        f = IsAdminFilter()
        event = make_event(config.FINANCE_ADMIN_ID)
        assert await f(event) is True

    async def test_unknown_user_blocked(self):
        f = IsAdminFilter()
        event = make_event(config.FINANCE_ADMIN_ID + 1)
        assert await f(event) is False

    async def test_zero_id_blocked(self):
        f = IsAdminFilter()
        event = make_event(0)
        assert await f(event) is False

    async def test_negative_id_blocked(self):
        f = IsAdminFilter()
        event = make_event(-1)
        assert await f(event) is False

    async def test_different_user_blocked(self):
        f = IsAdminFilter()
        event = make_event(999999999)
        assert await f(event) is False
