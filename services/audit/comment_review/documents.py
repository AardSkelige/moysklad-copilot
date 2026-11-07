import json
from datetime import datetime, timedelta

from core.logger import logger
from integrations.moysklad_audit import MoySkladAuditClient
from services.audit.context import delivery_method, format_moment
from services.audit.team_context import GLOSSARY, TEAM_CONTEXT
from services.llm_service import LLMClient
from integrations.moysklad_base import new_session
from services.audit.comment_review.common import (
    Progress,
    _STYLE,
    _ask_batches,
    _batches,
    _keep_author,
    _normalized,
    _only_final_dot,
    _short_reason,
    _too_fresh,
)
from services.audit.comment_review.demands import _review_demands


ENTITIES = {
    'supply': 'Приёмка',
    'enter': 'Оприходование',
    'loss': 'Списание',
    'move': 'Перемещение',
    'inventory': 'Инвентаризация',
    'demand': 'Отгрузка',
    'customerorder': 'Заказ покупателя',
    'purchaseorder': 'Заказ поставщику',
    'productiontask': 'ПЗ',
}


_SYSTEM = (
    f'{TEAM_CONTEXT}\n\n{GLOSSARY}\n\n{_STYLE}\n'
    'Ты — редактор комментариев к документам МойСклад компании.\n'
    'Все факты (номера заказов, трек-номера, суммы, причины) сохраняются дословно по смыслу. '
    'Для нулевых сумм и расхождений причина обязательна.\n\n'
    'Тебе дают список документов с текущими комментариями. Для каждого реши:\n'
    '• Комментарий уже полностью соответствует стандарту и стилю → "ok".\n'
    '• Иначе → "suggest": ПЕРЕПИШИ комментарий начисто — орфография, пунктуация, '
    'структура, стиль по правилам выше.\n'
    'КРИТИЧНО: new_comment должен РЕАЛЬНО отличаться от текущего текста; иначе верни "ok".\n'
    'reason — одно короткое конкретное предложение, ЧТО исправлено '
    '(например «опечатка: кодней → кондиционер; точка в конце»). '
    'НИКОГДА не пиши бессмыслицу вида «оплата вместо оплата» или «X → X».\n'
    'Если комментарий ПУСТ — верни "ok". Пустое поле это норма: комментарий нужен '
    'только там, где есть что сказать сверх самого документа. Пересказ полей '
    '(тип документа, контрагент, сумма, склад) — не комментарий, а шум: всё это '
    'владелец видит в документе. Никогда не сочиняй текст для пустого поля.\n'
    'НЕ выдумывай факты (причины, суммы, номера). Автор по зоне ответственности — '
    'не выдумка, а правило 6.\n\n'
    'Ответ — СТРОГО JSON-массив без markdown: '
    '[{"n": <номер>, "verdict": "ok"}, {"n": <номер>, "verdict": "suggest", '
    '"new_comment": "...", "reason": "..."}]'
)


async def collect_documents(days: int) -> tuple[list[dict], list[str]]:
    """Документы сгруппированы по типу (порядок ENTITIES), внутри — от старых к новым.

    Возвращает (документы, названия несобравшихся типов). Второй список нужен,
    чтобы владелец не увидел бодрое «все комментарии в порядке» после того, как
    МойСклад не ответил: молчаливый успех на пустом наборе — худший из отчётов.

    days <= 0 — вся история без фильтра по дате."""
    client = MoySkladAuditClient()
    filters = (f'moment>={format_moment(datetime.now() - timedelta(days=days))}'
               if days > 0 else '')
    docs, failed = [], []
    async with new_session() as session:
        for entity, label in ENTITIES.items():
            expand = 'agent,customerOrder' if entity == 'demand' else 'agent'
            try:
                rows = await client.list_entities(
                    session, entity,
                    filters=filters,
                    expand=expand, order='moment,asc',
                    max_rows=2000,
                )
            except Exception as e:
                logger.warning(f'[comments] {entity} не собрался: {e}')
                failed.append(label)
                continue
            for d in rows:
                doc = {
                    'entity': entity,
                    'id': d['id'],
                    'label': f'{label} №{d.get("name")} от {(d.get("moment") or "")[:10]}',
                    'agent': ((d.get('agent') or {}).get('name')
                              if isinstance(d.get('agent'), dict) else None),
                    'sum_rub': round(d.get('sum', 0) / 100, 2),
                    'comment': (d.get('description') or '').strip(),
                }
                if entity == 'demand' and _too_fresh(d.get('moment')):
                    # накладные расходы и комментарий часто вносят на следующий
                    # день — свежие отгрузки в ревью не берём, дойдут завтра
                    continue
                order = d.get('customerOrder') or {}
                if entity == 'demand' and order.get('id'):
                    # отгрузка без заказа (пока таких нет) пойдёт по общим правилам
                    doc.update({
                        'kind': 'demand',
                        'overhead_rub': round(
                            ((d.get('overhead') or {}).get('sum', 0)) / 100, 2),
                        'delivery_method': delivery_method(d),
                        'order_id': order['id'],
                        'order_name': order.get('name'),
                        'order_comment': (order.get('description') or '').strip(),
                    })
                docs.append(doc)
    return docs, failed


async def review_documents(docs: list[dict], llm: LLMClient | None = None,
                           progress: Progress = None) -> list[dict]:
    """Вернуть только документы с предложенной правкой (+ new_comment, reason).

    Отгрузки со связанным заказом идут через свой промпт (правила накладных расходов
    и перенос в заказ); их заказы проверяются ТОЙ ЖЕ карточкой-парой и из общего
    потока исключаются — иначе две карточки правили бы один комментарий вразнобой."""
    llm = llm or LLMClient()
    demands = [d for d in docs if d.get('kind') == 'demand']
    paired_orders = {d['order_id'] for d in demands}
    generic = [d for d in docs
               if d.get('kind') != 'demand'
               and not (d['entity'] == 'customerorder' and d['id'] in paired_orders)]
    # две фазы — один общий счётчик пачек для владельца
    total = len(_batches(generic)) + len(_batches(demands))
    first = len(_batches(generic))

    def shifted(offset: int) -> Progress:
        if not progress:
            return None
        return lambda done, _: progress(offset + done, total)

    suggestions = (await _review_generic(generic, llm, shifted(0))
                   + await _review_demands(demands, llm, shifted(first)))
    # вернуть порядок исходного списка (по типам, от старых к новым)
    pos = {(d['entity'], d['id']): i for i, d in enumerate(docs)}
    suggestions.sort(key=lambda s: pos.get((s['entity'], s['id']), 0))
    return suggestions


async def _review_generic(docs: list[dict], llm: LLMClient,
                          progress: Progress = None) -> list[dict]:
    suggestions = []
    batches = _batches(docs)
    payloads = [[{
        'n': i,
        'документ': d['label'],
        'контрагент': d['agent'],
        'сумма_руб': d['sum_rub'],
        'комментарий': d['comment'],
    } for i, d in enumerate(batch)] for batch in batches]
    answers = await _ask_batches(llm, _SYSTEM, payloads, 'comments', progress)
    for batch, verdicts in zip(batches, answers):
        if verdicts is None:
            continue
        for v in verdicts:
            if v.get('verdict') != 'suggest':
                continue
            new_comment = (v.get('new_comment') or '').strip()
            if not new_comment:
                continue
            try:
                doc = batch[int(v['n'])]
            except (KeyError, ValueError, IndexError):
                continue
            new_comment = _keep_author(doc['comment'], new_comment)
            # правка «только точка» уходит в пакетную операцию, не в карточку
            if _only_final_dot(doc['comment'], new_comment):
                continue
            # LLM иногда «предлагает» тот же текст — такие карточки бессмысленны
            if _normalized(new_comment) == _normalized(doc['comment']):
                continue
            # пустое поле — норма. Комментарий чиним, но не сочиняем: пересказ полей
            # документа («приёмка от X на Y руб.») не несёт ничего сверх самого документа
            if not doc['comment']:
                continue
            suggestions.append({**doc, 'new_comment': new_comment,
                                'reason': _short_reason(v.get('reason'))})
    return suggestions


_REFINE_SYSTEM = (
    f'{TEAM_CONTEXT}\n\n{GLOSSARY}\n\n{_STYLE}\n'
    'Ты — редактор комментариев к документам МойСклад. Тебе дают: исходный комментарий, '
    'твоё прежнее предложение и УКАЗАНИЕ владельца, что поправить. Сделай финальный '
    'вариант с учётом указания, соблюдая стиль. Факты сохраняй.\n'
    'Ответ — ТОЛЬКО текст финального комментария, без пояснений и кавычек.'
)


async def refine_comment(item: dict, instruction: str, llm: LLMClient | None = None) -> str:
    """Доработать предложение по замечанию владельца («упустил кондей», «допиши причину»)."""
    llm = llm or LLMClient()
    resp = await llm.chat([
        {'role': 'system', 'content': _REFINE_SYSTEM},
        {'role': 'user', 'content': json.dumps({
            'документ': item['label'],
            'исходный_комментарий': item['comment'],
            'моё_предложение': item['new_comment'],
            'указание_владельца': instruction,
        }, ensure_ascii=False)},
    ])
    text = (resp.get('content') or '').strip().strip('«»"')
    return _keep_author(item['comment'], text) if text else item['new_comment']
