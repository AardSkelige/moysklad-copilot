"""Обрыв связи на тяжёлой выборке — повод повторить запрос, а не ронять проверку."""

import asyncio

import pytest
from integrations.moysklad_base import MoySkladHTTP

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

class FlakySession:
    """Первые N запросов рвутся по таймауту, дальше отвечают."""
    def __init__(self, fails):
        self.fails, self.calls = fails, 0
    def request(self, *a, **kw):
        self.calls += 1
        outer = self
        class Ctx:
            async def __aenter__(self):
                if outer.calls <= outer.fails:
                    raise asyncio.TimeoutError()
                class R:
                    status = 200
                    async def json(self): return {'rows': [1]}
                return R()
            async def __aexit__(self, *a): return False
        return Ctx()

_real_sleep = asyncio.sleep


async def _no_wait(*_a, **_kw):
    await _real_sleep(0)


async def test_retries_after_connection_drop(monkeypatch):
    monkeypatch.setattr(asyncio, 'sleep', _no_wait)
    http = MoySkladHTTP(token='t')
    s = FlakySession(fails=2)
    assert await http.get(s, '/entity/demand') == {'rows': [1]}
    assert s.calls == 3

async def test_gives_up_after_limit(monkeypatch):
    monkeypatch.setattr(asyncio, 'sleep', _no_wait)
    http = MoySkladHTTP(token='t')
    s = FlakySession(fails=99)
    with pytest.raises(asyncio.TimeoutError):
        await http.get(s, '/entity/demand')
    assert s.calls == 3
