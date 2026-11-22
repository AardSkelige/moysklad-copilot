"""Ревью комментариев документов: LLM предлагает исправление, владелец решает.

Поток: собрать свежие документы → LLM пачками оценивает комментарии по стандарту →
владельцу по одной карточке «текущий / предлагаемый» с кнопками
[Заменить] [Оставить]. Замена пишется в МойСклад сразу (подтверждение = кнопка).
"""

from integrations.moysklad_audit import MoySkladAuditClient
from services.audit.comment_review.common import (
    DEMAND_MARKETPLACE_ZERO_COMMENT,
    apply_comment,
    _too_fresh,
)
from services.audit.comment_review.demands import _DEMAND_SYSTEM, apply_demand, refine_demand
from services.audit.comment_review.documents import (
    ENTITIES,
    _SYSTEM,
    collect_documents,
    refine_comment,
    review_documents,
)
from services.audit.comment_review.finance import (
    apply_finance,
    collect_finance_documents,
    refine_finance,
    review_finance_documents,
)
