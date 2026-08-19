"""Базовый HTTP-слой для МойСклад API 1.2.

Инкапсулирует грабли API, проверенные на живых запросах:
- Accept-Encoding: gzip обязателен (иначе 415);
- в filter= знаки > < и пробелы кодируются, = и ; остаются литеральными (иначе 400);
- лимиты: ~22 req/3s на пользовательский токен, отчёты весят 5, ≤5 параллельных.
"""

import asyncio
import socket
import ssl
from urllib.parse import quote

import aiohttp
import certifi

from core import config
from core.logger import logger

_PACE_SECONDS = 0.35       # ~3 запроса/сек — с запасом под вес отчётов
_MAX_CONCURRENT = 2
_RETRY_LIMIT = 3

# Тяжёлые выборки (все отгрузки с expand, обороты по товарам) отвечают долго,
# а без явных таймаутов aiohttp рвал их с ConnectionTimeoutError — на проде так
# падали проверки counterparty_balance, order_supply_mismatch, enter_price_vs_fifo.
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=180, connect=30, sock_read=120)


def new_session() -> aiohttp.ClientSession:
    """Сессия для МойСклада: явные таймауты и только IPv4.

    В контейнере IPv6-адрес api.moysklad.ru не резолвится (gaierror), а aiohttp
    по умолчанию пробует обе семьи и ждёт впустую."""
    connector = aiohttp.TCPConnector(family=socket.AF_INET, ttl_dns_cache=300, limit=10)
    return aiohttp.ClientSession(timeout=REQUEST_TIMEOUT, connector=connector)


def encode_filter(expression: str) -> str:
    """Кодировать значение параметра filter: '=' и ';' литеральные, остальное — percent-encoding."""
    return quote(expression, safe='=;')


def _ssl_ctx() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=certifi.where())


class _Retry429(Exception):
    """Лимит запросов: ждём столько, сколько просит API, и пробуем снова."""

    def __init__(self, pause: float):
        super().__init__(pause)
        self.pause = pause


# обрыв связи на тяжёлой выборке — не повод ронять проверку целиком
_RETRIABLE = (aiohttp.ClientConnectorError, aiohttp.ServerDisconnectedError,
              aiohttp.ClientPayloadError, asyncio.TimeoutError)


class MoySkladHTTP:
    """Троттлящий HTTP-клиент. Один экземпляр на прогон/сервис."""

    def __init__(self, token: str | None = None):
        self.base = config.MOYSKLAD_BASE_URL
        self._token = token or config.MOYSKLAD_TOKEN
        self._semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
        self._pace_lock = asyncio.Lock()
        self.request_count = 0

    def _headers(self) -> dict:
        return {
            'Authorization': f'Bearer {self._token}',
            'Accept': 'application/json;charset=utf-8',
            'Content-Type': 'application/json',
            'Accept-Encoding': 'gzip',
        }

    def url(self, path: str, query: str = '') -> str:
        base = path if path.startswith('http') else f'{self.base}{path}'
        return f'{base}?{query}' if query else base

    async def request(
        self,
        session: aiohttp.ClientSession,
        method: str,
        path: str,
        query: str = '',
        payload: dict | list | None = None,
    ) -> dict | list | None:
        url = self.url(path, query)
        async with self._semaphore:
            for attempt in range(_RETRY_LIMIT):
                async with self._pace_lock:
                    await asyncio.sleep(_PACE_SECONDS)
                try:
                    return await self._attempt(session, method, url, payload)
                except _RETRIABLE as e:
                    if attempt == _RETRY_LIMIT - 1:
                        raise
                    pause = 2 ** attempt
                    logger.warning(f'[ms] {type(e).__name__} на {url[:90]}, '
                                   f'повтор через {pause}с')
                    await asyncio.sleep(pause)
                except _Retry429 as e:
                    await asyncio.sleep(e.pause)
        raise RuntimeError(f'{method} {url[:150]} → 429 после {_RETRY_LIMIT} попыток')

    async def _attempt(self, session, method: str, url: str, payload):
        """Одна попытка запроса; 429 поднимает _Retry429, обрыв — сетевое исключение."""
        async with session.request(
            method, url, headers=self._headers(), json=payload, ssl=_ssl_ctx()
        ) as r:
            self.request_count += 1
            if r.status == 429:
                # X-Lognex-Retry-After — миллисекунды до сброса ограничения (см. md/_general.md)
                retry_after = float(r.headers.get('X-Lognex-Retry-After', 3000)) / 1000
                logger.warning(f'[ms] 429, ждём {retry_after:.1f}с: {url[:100]}')
                raise _Retry429(min(retry_after, 30))
            if r.status == 404:
                return None
            if r.status >= 400:
                text = await r.text()
                raise RuntimeError(f'{method} {url[:150]} → {r.status}: {text[:300]}')
            if r.status == 204:
                return {}
            return await r.json()

    async def get(self, session: aiohttp.ClientSession, path: str, query: str = '') -> dict | None:
        return await self.request(session, 'GET', path, query)

    async def put(self, session: aiohttp.ClientSession, path: str, payload: dict) -> dict | None:
        return await self.request(session, 'PUT', path, payload=payload)

    async def post(self, session: aiohttp.ClientSession, path: str, payload: dict) -> dict | None:
        return await self.request(session, 'POST', path, payload=payload)

    async def delete(self, session: aiohttp.ClientSession, path: str) -> None:
        await self.request(session, 'DELETE', path)
