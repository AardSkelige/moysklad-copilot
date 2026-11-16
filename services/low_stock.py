from dataclasses import dataclass
from decimal import Decimal
from html import escape

import aiohttp

from core import config
from integrations.moysklad_audit import MoySkladAuditClient

MESSAGE_LIMIT = 4000


@dataclass(frozen=True)
class LowStock:
    name: str
    stock: Decimal
    minimum: Decimal | None
    in_transit: Decimal
    uom: str


def _number(value) -> Decimal:
    return Decimal(str(value or 0))


def _entity_id(href: str) -> str:
    return href.split('?', 1)[0].rsplit('/', 1)[1]


def _top_folder(row: dict) -> str:
    folder = row.get('folder') or {}
    return (folder.get('pathName') or folder.get('name') or '').split('/')[0]


async def find_low_stock(client: MoySkladAuditClient, session: aiohttp.ClientSession) -> list[LowStock]:
    report = await client.stock_all(session, stock_mode='underMinimum', quantity_mode='all')
    rows = [row for row in report if _top_folder(row) not in config.STOCK_ALERT_SKIP_FOLDERS]
    if not rows:
        return []
    products = await client.list_entities(session, 'product', filters='archived=false', max_rows=5000)
    minimums = {p['id']: _number(p.get('minimumBalance')) for p in products}
    low = [
        LowStock(row['name'], _number(row.get('stock')),
                 minimums.get(_entity_id(row['meta']['href'])) or None,
                 _number(row.get('inTransit')), (row.get('uom') or {}).get('name', 'шт'))
        for row in rows
    ]
    return sorted(low, key=lambda item: item.name)


def _qty(value: Decimal) -> str:
    text = f'{value:,.3f}'.rstrip('0').rstrip('.').replace(',', ' ').replace('.', ',')
    return text or '0'


def _line(item: LowStock) -> str:
    line = f'• {escape(item.name)} — осталось {_qty(item.stock)} {escape(item.uom)}'
    if item.minimum is not None:
        line += f', порог {_qty(item.minimum)}'
    if item.in_transit:
        line += f', ожидается по заказам поставщикам {_qty(item.in_transit)}'
    return line


def format_alert(low: list[LowStock]) -> list[str]:
    messages, current = [], f'📉 <b>Ниже неснижаемого остатка: {len(low)}</b>\n'
    for item in low:
        line = _line(item)
        if len(current) + len(line) + 1 > MESSAGE_LIMIT:
            messages.append(current)
            current = ''
        current += '\n' + line
    messages.append(current)
    return messages
