"""Ревью комментариев документов: LLM предлагает исправление, владелец решает.

Поток: собрать свежие документы → LLM пачками оценивает комментарии по стандарту →
владельцу по одной карточке «текущий / предлагаемый» с кнопками
[Заменить] [Оставить]. Замена пишется в МойСклад сразу (подтверждение = кнопка).
"""

import json
import re
from datetime import datetime, timedelta

import aiohttp

from core import config
from core.logger import logger
from integrations.moysklad_audit import MoySkladAuditClient
from services.audit.context import delivery_method, format_moment
from services.audit.team_context import (
    AUTHOR_FINANCE,
    AUTHOR_MARKETPLACE,
    AUTHOR_PURCHASES,
    AUTHOR_SALES,
    FIN_COMMENT_EXAMPLE,
    FIN_PURPOSE_MARKETPLACE,
    GLOSSARY,
    MARKETPLACES,
    TEAM_CONTEXT,
)
from services.llm_service import LLMClient

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

_BATCH_SIZE = 8

_STYLE = (
    'Единый стиль (применяй ко ВСЕМ правкам одинаково, без исключений):\n'
    '1. Формат: «Имя: текст» — после двоеточия со строчной буквы (это продолжение фразы).\n'
    '2. В конце комментария — точка. Всегда.\n'
    '3. Одна мысль — одно предложение; переносы строк заменяй точками.\n'
    '4. Опечатки в словах предметки исправляй по словарю выше; сленг разворачивай '
    '(«кондей»/«кодней» → «кондиционер»).\n'
    '5. Дефис между словом и значением заменяй на тире с пробелами: '
    '«трек-номер - 123» → «трек-номер — 123» (внутри слов дефис остаётся: трек-номер).\n'
    f'6. Имя, которое УЖЕ написано в комментарии, — факт: сохраняй его дословно. '
    f'НИКОГДА не меняй одно имя на другое, даже если по зоне ответственности или '
    f'контрагенту ожидается кто-то иной: человек подписался сам, ему виднее. '
    f'Подставляй автора ТОЛЬКО когда имени в комментарии нет вообще — по зоне '
    f'ответственности и типу документа: приёмки, заказы поставщику, производство → '
    f'{AUTHOR_PURCHASES}; заказы покупателей → {AUTHOR_SALES}, НО если контрагент '
    f'{MARKETPLACES} → {AUTHOR_MARKETPLACE}; платежи и финансы → {AUTHOR_FINANCE}. '
    f'Только если тип документа не даёт определить — «⚠️ (автор?):».\n\n'
    f'Эталонные примеры (следуй им буквально):\n'
    f'• «трек-номер СДЭКа - 10261902800» (заказ покупателя, частный покупатель) → '
    f'«{AUTHOR_SALES}: трек-номер СДЭК — 10261902800.»\n'
    f'• «{AUTHOR_PURCHASES} приняла на склад производства\\nсамовывоз {AUTHOR_SALES}» → '
    f'«{AUTHOR_PURCHASES}: приняла на склад производства. Самовывоз — {AUTHOR_SALES}.»\n'
    f'• «Коррекция наличия, по базе 1 кодней глиттер, по факту 0» (списание, автор был '
    f'{AUTHOR_FINANCE}) → '
    f'«{AUTHOR_FINANCE}: коррекция наличия — по базе 1 кондиционер глиттер, по факту 0.»\n'
)

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
    'Если комментарий пуст — предложи фактический комментарий из данных документа '
    '(тип, контрагент, сумма), без выдуманных причин; автора подставь по правилу 6 стиля.\n'
    'НЕ выдумывай факты (причины, суммы, номера). Автор по зоне ответственности — '
    'не выдумка, а правило 6.\n\n'
    'Ответ — СТРОГО JSON-массив без markdown: '
    '[{"n": <номер>, "verdict": "ok"}, {"n": <номер>, "verdict": "suggest", '
    '"new_comment": "...", "reason": "..."}]'
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
    f'«Накладные расходы 0 — доставку оплачивает маркетплейс.» (контрагенты-маркетплейсы: '
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


def _too_fresh(moment: str | None) -> bool:
    """Отгрузке ещё не исполнилось суток — рано проверять комментарий."""
    try:
        doc_moment = datetime.strptime((moment or '')[:19], '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return False
    return doc_moment > datetime.now() - timedelta(hours=config.AUDIT_DEMAND_GRACE_HOURS)


async def collect_documents(days: int) -> list[dict]:
    """Документы сгруппированы по типу (порядок ENTITIES), внутри — от старых к новым.

    days <= 0 — вся история без фильтра по дате."""
    client = MoySkladAuditClient()
    filters = (f'moment>={format_moment(datetime.now() - timedelta(days=days))}'
               if days > 0 else '')
    docs = []
    async with aiohttp.ClientSession() as session:
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
    return docs


async def review_documents(docs: list[dict], llm: LLMClient | None = None) -> list[dict]:
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
    suggestions = (await _review_generic(generic, llm)
                   + await _review_demands(demands, llm))
    # вернуть порядок исходного списка (по типам, от старых к новым)
    pos = {(d['entity'], d['id']): i for i, d in enumerate(docs)}
    suggestions.sort(key=lambda s: pos.get((s['entity'], s['id']), 0))
    return suggestions


async def _review_generic(docs: list[dict], llm: LLMClient) -> list[dict]:
    suggestions = []
    for start in range(0, len(docs), _BATCH_SIZE):
        batch = docs[start:start + _BATCH_SIZE]
        payload = [{
            'n': i,
            'документ': d['label'],
            'контрагент': d['agent'],
            'сумма_руб': d['sum_rub'],
            'комментарий': d['comment'],
        } for i, d in enumerate(batch)]
        try:
            resp = await llm.chat([
                {'role': 'system', 'content': _SYSTEM},
                {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)},
            ])
            content = (resp.get('content') or '').strip()
            arr_start, arr_end = content.find('['), content.rfind(']')
            verdicts = json.loads(content[arr_start:arr_end + 1])
        except Exception as e:
            logger.warning(f'[comments] батч {start} не разобрался: {e}')
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
            # LLM иногда «предлагает» тот же текст — такие карточки бессмысленны
            if _normalized(new_comment) == _normalized(doc['comment']):
                continue
            suggestions.append({**doc, 'new_comment': new_comment,
                                'reason': _short_reason(v.get('reason'))})
    return suggestions


async def _review_demands(docs: list[dict], llm: LLMClient) -> list[dict]:
    """Отгрузки: new_comment может быть '' (очистить), new_order_comment — дополнение заказа."""
    suggestions = []
    orders_touched = set()   # заказ с двумя отгрузками правит только первая карточка
    for start in range(0, len(docs), _BATCH_SIZE):
        batch = docs[start:start + _BATCH_SIZE]
        payload = [{
            'n': i,
            'отгрузка': d['label'],
            'контрагент': d['agent'],
            'сумма_руб': d['sum_rub'],
            'накладные_расходы_руб': d['overhead_rub'],
            'способ_доставки': d.get('delivery_method'),
            'комментарий_отгрузки': d['comment'],
            'заказ': f'Заказ покупателя №{d["order_name"]}',
            'комментарий_заказа': d['order_comment'],
        } for i, d in enumerate(batch)]
        try:
            resp = await llm.chat([
                {'role': 'system', 'content': _DEMAND_SYSTEM},
                {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)},
            ])
            content = (resp.get('content') or '').strip()
            arr_start, arr_end = content.find('['), content.rfind(']')
            verdicts = json.loads(content[arr_start:arr_end + 1])
        except Exception as e:
            logger.warning(f'[comments] батч отгрузок {start} не разобрался: {e}')
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
            new_order_comment = (v.get('new_order_comment') or '').strip() or None
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
    async with aiohttp.ClientSession() as session:
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
        out['new_comment'] = data['new_comment'].strip()
    new_order_comment = (data.get('new_order_comment') or '').strip() or None
    if new_order_comment:
        out['new_order_comment'] = new_order_comment
    return out


def _normalized(text: str) -> str:
    return ' '.join((text or '').lower().split())


def _is_sdek(doc: dict) -> bool:
    return 'сдэк' in (doc.get('delivery_method') or '').lower()


def _short_reason(reason: str | None) -> str:
    """Обрезать пояснение по границе предложения, а не посреди слова."""
    text = (reason or '').strip()
    if len(text) <= 400:
        return text
    cut = text[:400]
    dot = cut.rfind('. ')
    return (cut[:dot + 1] if dot > 100 else cut.rsplit(' ', 1)[0] + '…')


async def apply_comment(entity: str, entity_id: str, new_comment: str):
    client = MoySkladAuditClient()
    async with aiohttp.ClientSession() as session:
        await client.update_entity(session, entity, entity_id,
                                   {'description': new_comment})


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
    '  - покупка без заказа: «Приёмка № X»;\n'
    '  - оплата от покупателя: «Заказ покупателя № X»;\n'
    f'  - выплата маркетплейса: {FIN_PURPOSE_MARKETPLACE};\n'
    '  - наличные с выставки: «Розничные продажи, выставка [название] [дата]»;\n'
    '  - операционка: короткая категория («Аренда», «Банковские услуги», «Бухгалтерия»).\n'
    '  В поле «связанные_документы» даны привязки платежа — используй их для точного '
    'основания. Если основание приходится УГАДЫВАТЬ (нет привязки) — начни назначение '
    'с «⚠️ » (владелец проверит). Если определить нельзя — верни null.\n'
    f'• КОММЕНТАРИЙ (description) — «Имя: человеческое пояснение» по стилю: после '
    f'двоеточия СТРОЧНАЯ буква, точка в конце, опечатки исправлены. '
    f'Эталон: «{FIN_COMMENT_EXAMPLE}» '
    f'Автор финансовых документов чаще {AUTHOR_FINANCE}, закупочных платежей — '
    f'{AUTHOR_PURCHASES}.\n\n'
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


async def collect_finance_documents(days: int = 0) -> list[dict]:
    """Финансовые документы с обоими полями и привязками. days<=0 — вся история."""
    client = MoySkladAuditClient()
    filters = (f'moment>={format_moment(datetime.now() - timedelta(days=days))}'
               if days > 0 else '')
    docs = []
    async with aiohttp.ClientSession() as session:
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
    return docs


async def review_finance_documents(docs: list[dict], llm: LLMClient | None = None) -> list[dict]:
    llm = llm or LLMClient()
    suggestions = []
    for start in range(0, len(docs), _BATCH_SIZE):
        batch = docs[start:start + _BATCH_SIZE]
        payload = [{
            'n': i,
            'документ': d['label'],
            'контрагент': d['agent'],
            'сумма_руб': d['sum_rub'],
            'назначение': d['purpose'],
            'комментарий': d['comment'],
            'связанные_документы': d['linked'],
            'назначение_авто_не_менять': bool(_PROTECTED_PURPOSE.match(d['purpose'])),
        } for i, d in enumerate(batch)]
        try:
            resp = await llm.chat([
                {'role': 'system', 'content': _FIN_SYSTEM},
                {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)},
            ])
            content = (resp.get('content') or '').strip()
            arr_start, arr_end = content.find('['), content.rfind(']')
            verdicts = json.loads(content[arr_start:arr_end + 1])
        except Exception as e:
            logger.warning(f'[fin-comments] батч {start} не разобрался: {e}')
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
            if new_comment and _normalized(new_comment) == _normalized(doc['comment']):
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
    async with aiohttp.ClientSession() as session:
        await client.update_entity(session, item['entity'], item['id'], payload)


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
    return text or item['new_comment']


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
        out['new_comment'] = new_comment
    return out
