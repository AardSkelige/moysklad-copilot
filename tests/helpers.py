"""Вспомогательные утилиты для тестов"""

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock


class MockFSMContext:
    """Мок FSMContext для unit-тестов"""

    def __init__(self, initial_data: dict | None = None, initial_state: str | None = None):
        self._data: dict = initial_data or {}
        self._state: str | None = initial_state

    async def get_data(self) -> dict:
        return dict(self._data)

    async def update_data(self, **kwargs) -> dict:
        self._data.update(kwargs)
        return dict(self._data)

    async def set_state(self, state) -> None:
        if state is None:
            self._state = None
        elif isinstance(state, str):
            self._state = state
        else:
            # aiogram State object — extract "Group:state" key
            self._state = state.state

    async def get_state(self) -> str | None:
        return self._state

    async def clear(self) -> None:
        self._data = {}
        self._state = None


def make_mock_message(
    text: str = '',
    user_id: int = 123456,
    chat_id: int = 123456,
) -> MagicMock:
    """Создаёт мок объекта Message"""
    msg = MagicMock()
    msg.text = text
    msg.from_user = MagicMock()
    msg.from_user.id = user_id
    msg.chat = MagicMock()
    msg.chat.id = chat_id
    msg.message_id = 1
    msg.answer = AsyncMock(return_value=MagicMock(message_id=2))
    msg.edit_text = AsyncMock()
    msg.bot = AsyncMock()
    return msg


def make_mock_callback(
    data: str = '',
    user_id: int = 123456,
    chat_id: int = 123456,
) -> MagicMock:
    """Создаёт мок объекта CallbackQuery"""
    cb = MagicMock()
    cb.data = data
    cb.from_user = MagicMock()
    cb.from_user.id = user_id
    cb.answer = AsyncMock()
    cb.message = make_mock_message(user_id=user_id, chat_id=chat_id)
    return cb


@asynccontextmanager
async def mock_session_scope_with(session):
    """Заменяет session_scope() для тестов с реальной сессией"""
    yield session
