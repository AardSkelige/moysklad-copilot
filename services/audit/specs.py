"""Спецификация проверки аудита и её находок.

Каждая проверка — класс-наследник CheckSpec: детерминированная детекция (detect),
человеческое объяснение (explain) и варианты исправления (fix_options, Фаза 2).
"""

import enum
import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


class Section(str, enum.Enum):
    PRODUCTS = 'Товары'
    PURCHASES = 'Закупки'
    WAREHOUSE = 'Склад'
    SALES = 'Продажи'
    PRODUCTION = 'Производство'
    MONEY = 'Деньги'
    CROSS = 'Общее'


class Severity(str, enum.Enum):
    CRITICAL = 'critical'
    IMPORTANT = 'important'
    WARNING = 'warning'


SEVERITY_EMOJI = {
    Severity.CRITICAL: '🔴',
    Severity.IMPORTANT: '🟠',
    Severity.WARNING: '🟡',
}


@dataclass
class RawFinding:
    entity_type: str
    entity_id: str
    entity_href: str
    entity_name: str
    severity: Severity
    payload: dict                # JSON-сериализуемо, всё для explain/fix
    fingerprint_salt: str = ''   # что делает находку уникальной кроме entity_id
    ui_link: str = ''            # meta.uuidHref — корректная ссылка в веб-интерфейс
                                 # (для товаров UI-id ≠ API-id, конструировать нельзя)


@dataclass
class FixOption:
    """Структурная кнопка исправления (применение — Фаза 2)."""
    fix_id: str
    label: str
    params: dict = field(default_factory=dict)


class CheckSpec(ABC):
    """Детектор СИГНАЛОВ, не судья: собирает кандидатов с фактами.

    Вердикт по нюансным случаям выносит LLM-аналитик (llm_triage=True) —
    он решает problem/ok/unclear, пишет объяснение и предлагает варианты.
    llm_triage=False только для математически однозначных ошибок
    (дубль платежа, отрицательный остаток)."""

    id: str
    section: Section
    title: str
    default_severity: Severity = Severity.IMPORTANT
    supports_incremental: bool = True   # умеет фильтр по updated>=since
    llm_triage: bool = True

    @abstractmethod
    async def detect(self, ctx, since: datetime | None) -> list[RawFinding]:
        """ctx — AuditContext. since=None означает полный скан."""

    @abstractmethod
    def explain(self, payload: dict) -> str:
        """Фолбэк-текст для Telegram, если LLM недоступен. HTML."""

    def fix_options(self, payload: dict) -> list[FixOption]:
        return []


def fingerprint(check_id: str, raw: RawFinding) -> str:
    body = f'{check_id}|{raw.entity_id}|{raw.fingerprint_salt}'
    return hashlib.sha256(body.encode()).hexdigest()[:32]


def web_link(entity_type: str, entity_id: str) -> str:
    """Ссылка на документ в веб-интерфейсе МойСклад."""
    ui_paths = {
        'supply': '#supply/edit?id=',
        'purchaseorder': '#purchaseorder/edit?id=',
        'demand': '#demand/edit?id=',
        'customerorder': '#customerorder/edit?id=',
        'enter': '#enter/edit?id=',
        'loss': '#loss/edit?id=',
        'move': '#move/edit?id=',
        'inventory': '#inventory/edit?id=',
        'salesreturn': '#salesreturn/edit?id=',
        'productiontask': '#productiontask/edit?id=',
        'paymentout': '#paymentout/edit?id=',
        'paymentin': '#paymentin/edit?id=',
        'cashout': '#cashout/edit?id=',
        'cashin': '#cashin/edit?id=',
        'product': '#good/edit?id=',
        'counterparty': '#company/edit?id=',
    }
    path = ui_paths.get(entity_type, f'#{entity_type}/edit?id=')
    return f'https://online.moysklad.ru/app/{path}{entity_id}'
