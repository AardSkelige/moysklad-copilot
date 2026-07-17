"""Контекст одного прогона аудита: клиент, aiohttp-сессия и кэш общих данных.

Кэш переживает один прогон: несколько проверок используют один и тот же
отчёт остатков или список документов без повторных запросов.
"""

from datetime import datetime, timedelta

import aiohttp

from core import config
from integrations.moysklad_audit import MoySkladAuditClient


def format_moment(dt: datetime) -> str:
    return dt.strftime('%Y-%m-%d %H:%M:%S')


def parse_moment(s: str) -> datetime:
    return datetime.strptime(s[:19], '%Y-%m-%d %H:%M:%S')


def delivery_method(doc: dict) -> str | None:
    """Значение доп. поля «Способ доставки» документа (None, если не заполнено)."""
    for a in (doc.get('attributes') or []):
        if a.get('name') == 'Способ доставки':
            v = a.get('value')
            return v.get('name') if isinstance(v, dict) else (str(v) if v else None)
    return None


class AuditContext:
    def __init__(self, client: MoySkladAuditClient, session: aiohttp.ClientSession):
        self.client = client
        self.session = session
        self.now = datetime.now()
        self._cache: dict = {}

    @property
    def scan_since_moment(self) -> str:
        """Нижняя граница полного скана (moment>=)."""
        return format_moment(self.now - timedelta(days=30 * config.AUDIT_SCAN_MONTHS))

    async def cached_list(self, key: str, entity: str, **kwargs) -> list[dict]:
        if key not in self._cache:
            self._cache[key] = await self.client.list_entities(self.session, entity, **kwargs)
        return self._cache[key]

    async def stock_fifo_map(self) -> dict[str, tuple[float, float]]:
        """href товара (без query) -> (FIFO-цена в копейках, остаток)."""
        if 'stock_fifo' not in self._cache:
            rows = await self.client.stock_all(self.session, stock_mode='all')
            self._cache['stock_fifo'] = {
                r.get('meta', {}).get('href', '').split('?')[0]: (r.get('price', 0), r.get('stock', 0))
                for r in rows
            }
        return self._cache['stock_fifo']
