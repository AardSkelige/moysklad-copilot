"""Generic-клиент МойСклад для модуля аудита.

Чтение — для детекторов и fix-агента; запись (update/delete) — ТОЛЬКО через
ErrorFixService после подтверждения владельцем кнопкой.
"""

from typing import AsyncIterator

import aiohttp

from integrations.moysklad_base import MoySkladHTTP, encode_filter

_EXPAND_PAGE_LIMIT = 100   # при expand МС ограничивает страницу сотней
_PAGE_LIMIT = 1000


class MoySkladAuditClient:
    """Все методы принимают внешнюю aiohttp-сессию — одна сессия на прогон аудита."""

    def __init__(self, http: MoySkladHTTP | None = None):
        self.http = http or MoySkladHTTP()

    async def list_entities(
        self,
        session: aiohttp.ClientSession,
        entity: str,
        *,
        filters: str = '',
        expand: str = '',
        order: str = '',
        max_rows: int = 1000,
    ) -> list[dict]:
        """Собрать строки списка с пагинацией. filters — сырое выражение ('moment>=...;applicable=true')."""
        limit = _EXPAND_PAGE_LIMIT if expand else _PAGE_LIMIT
        rows: list[dict] = []
        offset = 0
        while len(rows) < max_rows:
            parts = [f'limit={min(limit, max_rows - len(rows))}', f'offset={offset}']
            if filters:
                parts.append(f'filter={encode_filter(filters)}')
            if expand:
                parts.append(f'expand={expand}')
            if order:
                parts.append(f'order={order}')
            data = await self.http.get(session, f'/entity/{entity}', '&'.join(parts))
            page = (data or {}).get('rows', [])
            rows.extend(page)
            if len(page) < limit:
                break
            offset += len(page)
        return rows

    async def get_by_href(self, session: aiohttp.ClientSession, href: str) -> dict | None:
        return await self.http.get(session, href)

    async def stock_all(self, session: aiohttp.ClientSession, *, stock_mode: str = 'all') -> list[dict]:
        """Отчёт остатков (вес 5). stockMode передаётся внутри filter — иначе игнорируется."""
        data = await self.http.get(
            session, '/report/stock/all',
            f'limit=1000&filter={encode_filter(f"stockMode={stock_mode}")}',
        )
        return (data or {}).get('rows', [])

    async def stock_by_store_negative(self, session: aiohttp.ClientSession) -> list[dict]:
        data = await self.http.get(
            session, '/report/stock/bystore',
            f'limit=1000&filter={encode_filter("stockMode=negativeOnly")}',
        )
        return (data or {}).get('rows', [])

    async def entity_audit_events(
        self, session: aiohttp.ClientSession, entity: str, entity_id: str
    ) -> list[dict]:
        """События audit API с diff по полям — «что именно поменялось»."""
        data = await self.http.get(session, f'/entity/{entity}/{entity_id}/audit')
        return (data or {}).get('rows', [])

    async def get_positions(
        self, session: aiohttp.ClientSession, entity: str, entity_id: str
    ) -> list[dict]:
        data = await self.http.get(
            session, f'/entity/{entity}/{entity_id}/positions',
            f'expand=assortment&limit={_EXPAND_PAGE_LIMIT}',
        )
        return (data or {}).get('rows', [])

    # --- запись (только из ErrorFixService после подтверждения) ---

    async def update_entity(
        self, session: aiohttp.ClientSession, entity: str, entity_id: str, payload: dict
    ) -> dict | None:
        return await self.http.put(session, f'/entity/{entity}/{entity_id}', payload)

    async def update_position(
        self, session: aiohttp.ClientSession, entity: str, entity_id: str,
        position_id: str, payload: dict,
    ) -> dict | None:
        return await self.http.put(
            session, f'/entity/{entity}/{entity_id}/positions/{position_id}', payload)

    async def delete_entity(
        self, session: aiohttp.ClientSession, entity: str, entity_id: str
    ) -> None:
        await self.http.delete(session, f'/entity/{entity}/{entity_id}')
