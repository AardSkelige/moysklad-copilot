"""Текст ошибки для владельца в чате."""

import asyncio
from html import escape

import aiohttp


def user_error_text(e: Exception) -> str:
    """Причина сбоя человеческим языком и без HTML: текст со страницей ошибки
    МойСклада Telegram отклонял, и владелец оставался с вечным «Ищу…»."""
    if isinstance(e, (aiohttp.ClientError, asyncio.TimeoutError)):
        return 'нет связи с МойСкладом — повтори через несколько минут'
    return escape(str(e))[:300]
