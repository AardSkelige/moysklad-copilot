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


_DOC_LABELS = {
    'purchaseorder': 'Заказ поставщику',
    'customerorder': 'Заказ покупателя',
    'invoicein': 'Счёт поставщика',
    'invoiceout': 'Счёт покупателю',
    'paymentin': 'Входящий платёж',
    'paymentout': 'Исходящий платёж',
    'cashin': 'Приходный ордер',
    'cashout': 'Расходный ордер',
    'supply': 'Приёмка',
    'demand': 'Отгрузка',
}

# поля-связи, которые API отдаёт объектом при expand
_LINK_FIELDS = ('purchaseOrder', 'customerOrder', 'invoiceIn', 'invoiceOut', 'demand', 'supply')


def _linked_line(doc: dict) -> str | None:
    """«Заказ поставщику №00083 от 2026-08-12 на 2 000,00 ₽» из развёрнутой связи."""
    name = doc.get('name')
    if not name:
        return None   # связь пришла голой meta (без expand) — номера нет, показывать нечего
    label = _DOC_LABELS.get((doc.get('meta') or {}).get('type', ''), 'Документ')
    total = doc.get('sum')
    money = f' на {total / 100:,.2f} ₽'.replace(',', ' ').replace('.', ',') if isinstance(
        total, (int, float)) else ''
    return f'{label} №{name} от {(doc.get("moment") or "")[:10]}{money}'


def linked_documents(doc: dict) -> list[str]:
    """Связанные документы человекочитаемо — чтобы аналитик не гадал о связях по датам."""
    out = []
    for field in _LINK_FIELDS:
        v = doc.get(field)
        if isinstance(v, dict) and (line := _linked_line(v)):
            out.append(line)
    for v in (doc.get('payments') or []):
        if isinstance(v, dict) and (line := _linked_line(v)):
            out.append(line)
    return out


def is_marketplace(agent_name: str | None) -> bool:
    """Контрагент-маркетплейс: возит товар и рассчитывается по своим правилам.

    Импорт внутри функции — team_context подтягивает локальный override,
    а он читает переменные окружения при инициализации."""
    from services.audit.team_context import MARKETPLACE_AGENTS
    return bool(agent_name) and any(
        m.lower() in agent_name.lower() for m in MARKETPLACE_AGENTS)


def is_self_delivery(delivery: str | None) -> bool:
    """Самовывоз или доставка своими силами — накладных расходов не бывает."""
    from services.audit.team_context import SELF_DELIVERY_METHODS
    return bool(delivery) and any(
        m.lower() in delivery.lower() for m in SELF_DELIVERY_METHODS)


def is_internal_agent(agent_name: str | None) -> bool:
    """Служебный контрагент для внутренних передач (нулевые суммы там ожидаемы)."""
    from services.audit.team_context import INTERNAL_AGENTS
    return bool(agent_name) and any(
        a.lower() in agent_name.lower() for a in INTERNAL_AGENTS)


def is_consolidated_carrier(delivery: str | None) -> bool:
    """Перевозчик выставляет сводный счёт за период — парного платежа не ищем."""
    from services.audit.team_context import CONSOLIDATED_CARRIERS
    return bool(delivery) and any(
        c.lower() in delivery.lower() for c in CONSOLIDATED_CARRIERS)


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

    async def stock_rows(self) -> list[dict]:
        """Сырые строки отчёта остатков (вес 5) — один запрос на весь прогон."""
        if 'stock_rows' not in self._cache:
            self._cache['stock_rows'] = await self.client.stock_all(
                self.session, stock_mode='all')
        return self._cache['stock_rows']

    async def stock_fifo_map(self) -> dict[str, tuple[float, float]]:
        """href товара (без query) -> (FIFO-цена в копейках, остаток)."""
        if 'stock_fifo' not in self._cache:
            self._cache['stock_fifo'] = {
                r.get('meta', {}).get('href', '').split('?')[0]: (r.get('price', 0), r.get('stock', 0))
                for r in await self.stock_rows()
            }
        return self._cache['stock_fifo']

    async def uom_map(self) -> dict[str, str]:
        """href товара -> единица измерения («г», «мл», «шт»).

        Цены в МойСклад — за единицу товара, и для сырья это грамм или миллилитр.
        Без единицы цена «0,45 ₽» читается как цена за килограмм и вводит в заблуждение.
        """
        if 'uom' not in self._cache:
            self._cache['uom'] = {
                r.get('meta', {}).get('href', '').split('?')[0]:
                    ((r.get('uom') or {}).get('name') or '').strip()
                for r in await self.stock_rows()
            }
        return self._cache['uom']
