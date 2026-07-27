from services.audit.checks.cross import DeliveryAsPositionCheck, RetroEditCheck, StaleDocsCheck
from services.audit.checks.money import (
    CounterpartyBalanceCheck, PaymentDuplicateCheck, PaymentNoClosingDocsCheck,
)
from services.audit.checks.production import ProductionRetroEditCheck, ProductionStuckCheck
from services.audit.checks.products import FifoDeviationCheck, FifoZeroCheck
from services.audit.checks.purchases import OrderSupplyMismatchCheck, SupplyZeroPriceCheck
from services.audit.checks.sales import (
    DemandNoOverheadCheck, DemandOverheadPaymentCheck, DemandZeroCheck,
)
from services.audit.checks.warehouse import EnterPriceVsFifoCheck, NegativeStockCheck
from services.audit.specs import CheckSpec


_BY_ID: dict[str, CheckSpec] | None = None


def registry_by_id() -> dict[str, CheckSpec]:
    """Проверка по её id — для фолбэк-текста notifier'а, когда вердикта LLM нет."""
    global _BY_ID
    if _BY_ID is None:
        _BY_ID = {c.id: c for c in build_registry()}
    return _BY_ID


def build_registry() -> list[CheckSpec]:
    return [
        SupplyZeroPriceCheck(),
        OrderSupplyMismatchCheck(),
        PaymentDuplicateCheck(),
        CounterpartyBalanceCheck(),
        PaymentNoClosingDocsCheck(),
        DeliveryAsPositionCheck(),
        EnterPriceVsFifoCheck(),
        NegativeStockCheck(),
        FifoZeroCheck(),
        FifoDeviationCheck(),
        DemandZeroCheck(),
        DemandNoOverheadCheck(),
        DemandOverheadPaymentCheck(),
        RetroEditCheck(),
        ProductionRetroEditCheck(),
        ProductionStuckCheck(),
        StaleDocsCheck(),
    ]
