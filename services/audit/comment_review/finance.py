import json
import re
from datetime import datetime, timedelta

from core.logger import logger
from integrations.moysklad_audit import MoySkladAuditClient
from services.audit.context import format_moment
from services.audit.team_context import (
    AUTHOR_FINANCE,
    AUTHOR_PURCHASES,
    FIN_COMMENT_EXAMPLE,
    FIN_PURPOSE_MARKETPLACE,
    GLOSSARY,
    TEAM_CONTEXT,
)
from services.llm_service import LLMClient
from integrations.moysklad_base import new_session
from services.audit.comment_review.common import (
    Progress,
    _ask_batches,
    _batches,
    _keep_author,
    _normalized,
    _only_final_dot,
    _short_reason,
)


# ============================ финансовые документы ============================

FIN_ENTITIES = {
    'paymentout': 'Исходящий платёж',
    'paymentin': 'Входящий платёж',
    'cashout': 'РКО',
    'cashin': 'ПКО',
}

_RU_OP_TYPE = {
    'purchaseorder': 'Заказ поставщику', 'customerorder': 'Заказ покупателя',
    'supply': 'Приёмка', 'demand': 'Отгрузка', 'invoicein': 'Счёт поставщика',
    'invoiceout': 'Счёт покупателю', 'salesreturn': 'Возврат покупателя',
    'commissionreportin': 'Отчёт комиссионера', 'commissionreportout': 'Отчёт комиссионера',
}

# Назначения, которые НЕ трогаем: авто-тексты МойСклад и метки финансового модуля бота
_PROTECTED_PURPOSE = re.compile(r'^(Оплата по |Возврат по |Приход по |Telegram \|)')

_FIN_SYSTEM = (
    f'{TEAM_CONTEXT}\n\n{GLOSSARY}\n\n'
    'Ты — редактор ФИНАНСОВЫХ документов МойСклад (платежи, ПКО/РКО) компании.\n'
    'У каждого документа два поля:\n'
    '• НАЗНАЧЕНИЕ (paymentPurpose) — ссылка на основание платежа. Стандарт:\n'
    '  - доставка: «Доставка по приёмке № X»;\n'
    '  - оплата поставщику по заказу: «Заказ поставщику № X» — если платёж связан '
    'с заказом поставщику, основание именно заказ, а НЕ приёмка;\n'
    '  - покупка без заказа: «Приёмка № X»;\n'
    '  - оплата от покупателя: «Заказ покупателя № X»;\n'
    f'  - выплата маркетплейса: {FIN_PURPOSE_MARKETPLACE};\n'
    '  - наличные с выставки: «Розничные продажи, выставка [название] [дата]»;\n'
    '  - операционка: короткая категория («Аренда», «Банковские услуги», «Бухгалтерия»).\n'
    '  В поле «связанные_документы» даны привязки платежа — используй их для точного '
    'основания. Знак «⚠️ » ставь ТОЛЬКО когда сам придумываешь основание и можешь '
    'ошибиться. Уже написанное осмысленное назначение знаком не помечай: отсутствие '
    'привязки само по себе не повод. Если определить нельзя — верни null.\n'
    f'• КОММЕНТАРИЙ (description) — «Имя: человеческое пояснение» по стилю: после '
    f'двоеточия СТРОЧНАЯ буква, точка в конце, опечатки исправлены. '
    f'Эталон: «{FIN_COMMENT_EXAMPLE}» '
    f'Автор финансовых документов чаще {AUTHOR_FINANCE}, закупочных платежей — '
    f'{AUTHOR_PURCHASES}.\n\n'
    'РКО и оплата картой/наличными — это ЛИЧНЫЕ деньги того, кто подписался '
    '(совладельцы платят своими, компенсации нет). Комментарий там обязан говорить, '
    'чьи это деньги, и способ, если он указан в исходном тексте: «Яна: купила '
    'на свои, наличными.», «Женя: оплатила доставку своими.». Что именно куплено '
    'и по какому документу — не пиши, это видно в позициях и назначении.\n'
    'Исходящий платёж с расчётного счёта — деньги компании, чьи они, писать не нужно.\n'
    'Комментарий НЕ повторяет назначение: если сказать нечего сверх основания платежа, '
    'верни new_comment=null. Пустой комментарий — норма, сочинять текст для него нельзя. '
    'Комментарий нужен только для того, чего в документе не видно: чьи это деньги '
    '(сотрудник платил своими), необычный способ оплаты, оплата за третье лицо.\n'
    'Если у документа поле «назначение_авто_не_менять» = true — назначение создано '
    'системой, для него всегда new_purpose=null; НЕ упоминай это в reason, слово '
    '«защищено» не используй — просто предлагай только комментарий.\n'
    'reason — ОДНО короткое предложение о сути правки, без пересказа правил и '
    'служебных полей.\n'
    'Для каждого документа верни JSON-объект. Если поле не требует правки или '
    'править нельзя — верни для него null.\n'
    'Ответ — СТРОГО JSON-массив: [{"n": 0, "new_purpose": "..."|null, '
    '"new_comment": "..."|null, "reason": "..."}]. Если оба поля null — объект можно опустить.'
)


async def collect_finance_documents(days: int = 0) -> tuple[list[dict], list[str]]:
    """Финансовые документы с обоими полями и привязками. days<=0 — вся история.

    Возвращает (документы, названия несобравшихся типов) — см. collect_documents."""
    client = MoySkladAuditClient()
    filters = (f'moment>={format_moment(datetime.now() - timedelta(days=days))}'
               if days > 0 else '')
    docs, failed = [], []
    async with new_session() as session:
        for entity, label in FIN_ENTITIES.items():
            try:
                rows = await client.list_entities(
                    session, entity,
                    filters=filters,
                    expand='agent,operations', order='moment,asc',
                    max_rows=2000,
                )
            except Exception as e:
                logger.warning(f'[fin-comments] {entity} не собрался: {e}')
                failed.append(label)
                continue
            for d in rows:
                linked = []
                for op in (d.get('operations') or []):
                    op_type = (op.get('meta') or {}).get('type', '')
                    if op.get('name'):
                        ru = _RU_OP_TYPE.get(op_type, op_type)
                        linked.append(f'{ru} № {op["name"]}')
                docs.append({
                    'kind': 'finance',
                    'entity': entity,
                    'id': d['id'],
                    'label': f'{label} №{d.get("name")} от {(d.get("moment") or "")[:10]}',
                    'agent': ((d.get('agent') or {}).get('name')
                              if isinstance(d.get('agent'), dict) else None),
                    'sum_rub': round(d.get('sum', 0) / 100, 2),
                    'purpose': (d.get('paymentPurpose') or '').strip(),
                    'comment': (d.get('description') or '').strip(),
                    'linked': linked,
                })
    return docs, failed


async def review_finance_documents(docs: list[dict], llm: LLMClient | None = None,
                                   progress: Progress = None) -> list[dict]:
    llm = llm or LLMClient()
    suggestions = []
    batches = _batches(docs)
    payloads = [[{
        'n': i,
        'документ': d['label'],
        'контрагент': d['agent'],
        'сумма_руб': d['sum_rub'],
        'назначение': d['purpose'],
        'комментарий': d['comment'],
        'связанные_документы': d['linked'],
        'назначение_авто_не_менять': bool(_PROTECTED_PURPOSE.match(d['purpose'])),
    } for i, d in enumerate(batch)] for batch in batches]
    answers = await _ask_batches(llm, _FIN_SYSTEM, payloads, 'fin-comments', progress)
    for batch, verdicts in zip(batches, answers):
        if verdicts is None:
            continue
        for v in verdicts:
            try:
                doc = batch[int(v['n'])]
            except (KeyError, ValueError, IndexError, TypeError):
                continue
            new_purpose = (v.get('new_purpose') or '').strip() or None
            new_comment = (v.get('new_comment') or '').strip() or None
            # страховка кодом: защищённые назначения не переписываем, что бы LLM ни решил
            if new_purpose and _PROTECTED_PURPOSE.match(doc['purpose']):
                new_purpose = None
            if new_purpose and _normalized(new_purpose) == _normalized(doc['purpose']):
                new_purpose = None
            if new_comment:
                new_comment = _keep_author(doc['comment'], new_comment)
            if new_comment and _only_final_dot(doc['comment'], new_comment):
                new_comment = None
            if new_comment and _normalized(new_comment) == _normalized(doc['comment']):
                new_comment = None
            # комментарий не сочиняем из пустоты — в отличие от назначения,
            # которое восстанавливается по привязкам платежа
            if new_comment and not doc['comment']:
                new_comment = None
            if not new_purpose and not new_comment:
                continue
            suggestions.append({**doc, 'new_purpose': new_purpose,
                                'new_comment': new_comment,
                                'reason': _short_reason(v.get('reason'))})
    return suggestions


async def apply_finance(item: dict):
    payload = {}
    if item.get('new_purpose'):
        payload['paymentPurpose'] = item['new_purpose']
    if item.get('new_comment'):
        payload['description'] = item['new_comment']
    if not payload:
        return
    client = MoySkladAuditClient()
    async with new_session() as session:
        await client.update_entity(session, item['entity'], item['id'], payload)


_FIN_REFINE_SYSTEM = (
    _FIN_SYSTEM +
    '\n\nСЕЙЧАС РЕЖИМ ДОРАБОТКИ: тебе дают один документ, твои прежние предложения '
    'и УКАЗАНИЕ владельца. Верни финальные варианты с учётом указания. '
    'Ответ — СТРОГО один JSON-объект: {"new_purpose": "..."|null, "new_comment": "..."|null}.'
)


async def refine_finance(item: dict, instruction: str, llm: LLMClient | None = None) -> dict:
    """Доработать оба поля финансового документа по замечанию владельца."""
    llm = llm or LLMClient()
    resp = await llm.chat([
        {'role': 'system', 'content': _FIN_REFINE_SYSTEM},
        {'role': 'user', 'content': json.dumps({
            'документ': item['label'],
            'контрагент': item['agent'],
            'сумма_руб': item['sum_rub'],
            'назначение_сейчас': item['purpose'],
            'комментарий_сейчас': item['comment'],
            'моё_предложение_назначение': item.get('new_purpose'),
            'моё_предложение_комментарий': item.get('new_comment'),
            'связанные_документы': item.get('linked', []),
            'указание_владельца': instruction,
        }, ensure_ascii=False)},
    ])
    content = (resp.get('content') or '').strip()
    try:
        start, end = content.find('{'), content.rfind('}')
        data = json.loads(content[start:end + 1])
    except (ValueError, json.JSONDecodeError):
        return item
    new_purpose = (data.get('new_purpose') or '').strip() or None
    if new_purpose and _PROTECTED_PURPOSE.match(item['purpose']):
        new_purpose = None
    out = dict(item)
    if new_purpose:
        out['new_purpose'] = new_purpose
    new_comment = (data.get('new_comment') or '').strip() or None
    if new_comment:
        out['new_comment'] = _keep_author(item['comment'], new_comment)
    return out
