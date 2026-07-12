"""Коды товаров, которые НЕ предлагать в производственном агенте.

Эти позиции физически ещё есть на складе (нельзя архивировать в МС),
но снимаются с производства и не должны попадать в кандидаты при поиске.

Список живёт в .env: PRODUCTION_EXCLUDED_CODES — коды через запятую.
Никаких миграций не нужно, правка применяется на следующем перезапуске бота.
"""

import os

EXCLUDED_PRODUCT_CODES: frozenset[str] = frozenset(
    code.strip()
    for code in os.getenv('PRODUCTION_EXCLUDED_CODES', '').split(',')
    if code.strip()
)
