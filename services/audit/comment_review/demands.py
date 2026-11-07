import json

from integrations.moysklad_audit import MoySkladAuditClient
from services.audit.context import is_marketplace
from services.audit.team_context import (
    AUTHOR_MARKETPLACE,
    AUTHOR_SALES,
    GLOSSARY,
    MARKETPLACES,
    TEAM_CONTEXT,
)
from services.llm_service import LLMClient
from integrations.moysklad_base import new_session
from services.audit.comment_review.common import (
    DEMAND_MARKETPLACE_ZERO_COMMENT,
    Progress,
    _ask_batches,
    _batches,
    _keep_author,
    _normalized,
    _only_final_dot,
    _short_reason,
)


# Отгрузки живут по своим правилам: вся информация — в связанном заказе покупателя,
# комментарий отгрузки — только пояснение накладных расходов. Решение владельца 08.07.2026.
_DEMAND_SYSTEM = (
    f'{TEAM_CONTEXT}\n\n{GLOSSARY}\n\n'
    'Ты — редактор комментариев ОТГРУЗОК МойСклад компании.\n'
    'Каждая отгрузка связана с заказом покупателя. Правило компании: вся информация '
    'о договорённостях, способе передачи, трек-номерах и причинах живёт в комментарии '
    'ЗАКАЗА; комментарий ОТГРУЗКИ — только про накладные расходы (доставка, комиссии, '
    'оплаченные компанией).\n\n'
    'Правила для комментария отгрузки (new_comment):\n'
    '• Накладные расходы = 0 → комментарий обязан объяснять почему, единым форматом '
    f'«Накладные расходы 0 — причина.»: «Накладные расходы 0 — самовывоз.», '
    f'«{DEMAND_MARKETPLACE_ZERO_COMMENT}» (контрагенты-маркетплейсы: '
    f'{MARKETPLACES}), «Накладные расходы 0 — доставку оплачивал получатель.» '
    'Причину собери из комментариев отгрузки и заказа и контрагента; '
    'если причины нигде нет — new_comment оставь null, НЕ выдумывай.\n'
    '• ЗАПРЕЩЕНО очищать (new_comment="") комментарий, который уже поясняет '
    'накладные расходы («Накладные расходы 0 — самовывоз.», «За доставку платила …») — '
    'если он уже соответствует стандарту, верни new_comment=null.\n'
    '• Способ доставки СДЭК — особый случай: СДЭК выставляет единый сводный счёт '
    'за период, поэтому пояснять накладные расходы НЕ НУЖНО ни при нуле, ни при '
    'сумме > 0. В пустой комментарий такой отгрузки ничего не добавляй; уместны '
    'только правка стиля существующего текста и перенос лишнего в заказ.\n'
    '• Накладные расходы > 0 → комментарий НЕ НУЖЕН: если после удаления дублей '
    'и перенесённого ничего не остаётся — new_comment = "" (пустой). '
    'Исключение: пояснение самих накладных, УЖЕ написанное в исходных комментариях '
    f'(кто платил, из чего состоит сумма), остаётся в отгрузке в стиле '
    f'«За доставку платила {AUTHOR_SALES} со своего счёта.» — такое в заказ не переносить.\n'
    '• ЗАПРЕЩЕНО придумывать природу накладных расходов: если в исходных текстах '
    'не написано «комиссия банка» или «доставка» — НЕ пиши этого. Сумму накладных '
    'в комментарий не переписывай, она и так в поле документа.\n'
    '• Имя автора в комментарии отгрузки НЕ пишется — кто отгружал, неважно. '
    'Просто предложение с заглавной буквы и точкой в конце, опечатки по словарю.\n'
    '• Бессодержательные пересказы полей документа («отгрузка №X, контрагент Y, '
    'сумма Z руб.») удаляй: new_comment = "".\n\n'
    'Правила для комментария заказа (new_order_comment):\n'
    '• Всё из комментария отгрузки, что НЕ про накладные расходы (треки, даты, '
    'способы передачи, договорённости), переносится в заказ: верни new_order_comment — '
    'ПОЛНЫЙ новый текст комментария заказа. Существующие факты заказа сохраняй '
    'по смыслу дословно, недостающие добавляй; весь текст приведи к стилю '
    '«Имя: текст со строчной», одна мысль — одно предложение, точка в конце, '
    'тире вместо дефиса у значений.\n'
    '• Комментарий заказа проверяешь ТЫ ЖЕ (отдельно заказ не проверяется): даже если '
    f'переносить нечего, приведи его к стилю — «Имя: текст со строчной», точка в конце, '
    f'опечатки по словарю; если автора НЕТ — {AUTHOR_SALES}, а для маркетплейсов '
    f'({MARKETPLACES}) — {AUTHOR_MARKETPLACE}. Уже указанное имя не подменяй: '
    f'написано «{AUTHOR_SALES}» в заказе маркетплейса — так и оставь. '
    f'Если комментарий заказа уже в порядке и переносить нечего — null.\n'
    '• Если факт из отгрузки уже есть в заказе — просто убери дубль из отгрузки.\n'
    '• Комментарий заказа никогда не очищай и не сокращай — только дополняй и правь стиль.\n\n'
    'ГЛАВНОЕ: ни один факт не теряется. Убирая текст из отгрузки, убедись, что факт '
    'либо уже есть в комментарии заказа, либо добавлен тобой в new_order_comment. '
    'Удалять без переноса можно ТОЛЬКО пересказы полей документа.\n\n'
    'Для каждого документа верни объект: {"n": <номер>, "new_comment": "..."|""|null, '
    '"new_order_comment": "..."|null, "reason": "..."}. '
    'null = поле не менять; new_comment="" = очистить комментарий отгрузки. '
    'Если менять нечего — оба поля null. reason — одно короткое предложение о сути правки.\n'
    'НЕ выдумывай факты (треки, суммы, причины).\n'
    'Ответ — СТРОГО JSON-массив без markdown.'
)


async def _review_demands(docs: list[dict], llm: LLMClient,
                          progress: Progress = None) -> list[dict]:
    """Отгрузки: new_comment может быть '' (очистить), new_order_comment — дополнение заказа."""
    suggestions = []
    orders_touched = set()   # заказ с двумя отгрузками правит только первая карточка
    batches = _batches(docs)
    payloads = [[{
        'n': i,
        'отгрузка': d['label'],
        'контрагент': d['agent'],
        'сумма_руб': d['sum_rub'],
        'накладные_расходы_руб': d['overhead_rub'],
        'способ_доставки': d.get('delivery_method'),
        'комментарий_отгрузки': d['comment'],
        'заказ': f'Заказ покупателя №{d["order_name"]}',
        'комментарий_заказа': d['order_comment'],
    } for i, d in enumerate(batch)] for batch in batches]
    answers = await _ask_batches(llm, _DEMAND_SYSTEM, payloads, 'comments', progress)
    for batch, verdicts in zip(batches, answers):
        if verdicts is None:
            continue
        for v in verdicts:
            try:
                doc = batch[int(v['n'])]
            except (KeyError, ValueError, IndexError, TypeError):
                continue
            # None = не менять; '' = очистить комментарий отгрузки
            new_comment = v.get('new_comment')
            if new_comment is not None:
                new_comment = new_comment.strip()
                if _normalized(new_comment) == _normalized(doc['comment']):
                    new_comment = None
                # страховки кодом, что бы LLM ни решил:
                elif not new_comment and 'накладн' in _normalized(doc['comment']):
                    # пояснение накладных расходов не стираем
                    new_comment = None
                elif new_comment and not doc['comment'] and _is_sdek(doc):
                    # СДЭК: единый сводный счёт — в пустую отгрузку ничего не пишем
                    new_comment = None
                elif new_comment and _goes_to_marketplace_batch(doc):
                    new_comment = None
            new_order_comment = (v.get('new_order_comment') or '').strip() or None
            if new_order_comment:
                new_order_comment = _keep_author(doc['order_comment'], new_order_comment)
            if new_order_comment and _only_final_dot(doc['order_comment'], new_order_comment):
                new_order_comment = None
            # заказ только дополняем — очистку и «тот же текст» отбрасываем
            if new_order_comment and _normalized(new_order_comment) == _normalized(doc['order_comment']):
                new_order_comment = None
            if new_order_comment and doc['order_id'] in orders_touched:
                new_order_comment = None
            if new_comment is None and new_order_comment is None:
                continue
            if new_order_comment:
                orders_touched.add(doc['order_id'])
            suggestions.append({**doc, 'new_comment': new_comment,
                                'new_order_comment': new_order_comment,
                                'reason': _short_reason(v.get('reason'))})
    return suggestions


async def apply_demand(item: dict):
    """Записать до двух документов: комментарий отгрузки и/или связанного заказа."""
    client = MoySkladAuditClient()
    async with new_session() as session:
        if item.get('new_comment') is not None:
            await client.update_entity(session, 'demand', item['id'],
                                       {'description': item['new_comment']})
        if item.get('new_order_comment'):
            await client.update_entity(session, 'customerorder', item['order_id'],
                                       {'description': item['new_order_comment']})


_DEMAND_REFINE_SYSTEM = (
    _DEMAND_SYSTEM +
    '\n\nСЕЙЧАС РЕЖИМ ДОРАБОТКИ: тебе дают одну отгрузку, твои прежние предложения '
    'и УКАЗАНИЕ владельца. Верни финальные варианты с учётом указания. '
    'Ответ — СТРОГО один JSON-объект: {"new_comment": "..."|""|null, '
    '"new_order_comment": "..."|null}.'
)


async def refine_demand(item: dict, instruction: str, llm: LLMClient | None = None) -> dict:
    """Доработать предложения по отгрузке и её заказу по замечанию владельца."""
    llm = llm or LLMClient()
    resp = await llm.chat([
        {'role': 'system', 'content': _DEMAND_REFINE_SYSTEM},
        {'role': 'user', 'content': json.dumps({
            'отгрузка': item['label'],
            'контрагент': item['agent'],
            'сумма_руб': item['sum_rub'],
            'накладные_расходы_руб': item['overhead_rub'],
            'способ_доставки': item.get('delivery_method'),
            'комментарий_отгрузки_сейчас': item['comment'],
            'заказ': f'Заказ покупателя №{item["order_name"]}',
            'комментарий_заказа_сейчас': item['order_comment'],
            'моё_предложение_отгрузка': item.get('new_comment'),
            'моё_предложение_заказ': item.get('new_order_comment'),
            'указание_владельца': instruction,
        }, ensure_ascii=False)},
    ])
    content = (resp.get('content') or '').strip()
    try:
        start, end = content.find('{'), content.rfind('}')
        data = json.loads(content[start:end + 1])
    except (ValueError, json.JSONDecodeError):
        return item
    out = dict(item)
    if 'new_comment' in data and data['new_comment'] is not None:
        out['new_comment'] = _keep_author(item['comment'], data['new_comment'].strip())
    new_order_comment = (data.get('new_order_comment') or '').strip() or None
    if new_order_comment:
        out['new_order_comment'] = _keep_author(item['order_comment'], new_order_comment)
    return out


def _goes_to_marketplace_batch(doc: dict) -> bool:
    return (not doc['comment'] and not doc.get('overhead_rub')
            and is_marketplace(doc.get('agent')))


def _is_sdek(doc: dict) -> bool:
    return 'сдэк' in (doc.get('delivery_method') or '').lower()
