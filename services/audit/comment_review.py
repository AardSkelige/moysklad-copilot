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
    CANONICAL_NAMES,
    FIN_COMMENT_EXAMPLE,
    FIN_PURPOSE_MARKETPLACE,
    GLOSSARY,
    MARKETPLACES,
    TEAM_CONTEXT,
)
from services.llm_service import LLMClient
from integrations.moysklad_base import new_session

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
    f'4a. Имя автора пиши в эталонной форме: {", ".join(CANONICAL_NAMES)}. Это только '
    'написание («Сережа» → «Серёжа»); подменять одного человека другим НЕЛЬЗЯ.\n'
    '4b. Правь минимально. Обязательны лишь формат «Имя: текст» и точка в конце; '
    'всё остальное авторское оставляй как есть — сокращения и пометки самого сотрудника '
    '(«+ ПАКЕТЫ», «+ ОТКРЫТКИ», «ШК»), заглавные буквы, порядок слов, формулировки. '
    'НЕ разворачивай «+» в «плюс», НЕ понижай регистр в перечислениях, НЕ переписывай '
    'понятную фразу ради гладкости. Предлагай правку, только когда есть содержательная '
    'причина: нет имени, слиплись строки, опечатка в предметном слове, текст непонятен '
    'без документа или потерян факт. Косметика без такой причины — это лишняя правка, '
    'верни verdict «ok».\n'
    '5. Дефис между словом и значением заменяй на тире с пробелами: '
    '«трек-номер - 123» → «трек-номер — 123» (внутри слов дефис остаётся: трек-номер).\n'
    '5a. Идентификаторы — номера заказов и отгрузок, трек-номера, артикулы, коды, '
    'суммы и даты — переноси СИМВОЛ В СИМВОЛ. Ничего не отбрасывай и не «причёсывай»: '
    '«94709764-0166-1й» так и остаётся «94709764-0166-1й», «№ 00077 от 03.07.2026» '
    'не сокращается до «№ 00077». Это данные, а не стиль.\n'
    f'6. ИМЯ В КОММЕНТАРИИ НЕПРИКОСНОВЕННО. Если текст начинается с имени, оно '
    f'остаётся тем же самым — всегда, без единого исключения. Ни зона ответственности, '
    f'ни контрагент, ни тип документа НЕ являются поводом заменить одно имя другим: '
    f'человек подписался сам, и это факт, а не стиль. Замена имени — грубая ошибка, '
    f'даже если по всем правилам компании ожидается кто-то иной. Разрешено только '
    f'исправить написание того же имени ({", ".join(CANONICAL_NAMES)}).\n'
    f'6a. Подставлять автора можно ТОЛЬКО в комментарий, где имени нет вообще. '
    f'Тогда бери его по типу документа: приёмки, заказы поставщику, производство → '
    f'{AUTHOR_PURCHASES}; заказы покупателей → {AUTHOR_SALES}, но при контрагенте '
    f'{MARKETPLACES} → {AUTHOR_MARKETPLACE}; платежи и финансы → {AUTHOR_FINANCE}. '
    f'Если тип документа не даёт определить — «⚠️ (автор?):».\n\n'
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


def _normalized(text: str) -> str:
    return ' '.join((text or '').lower().split())


def _only_final_dot(current: str, proposed: str) -> bool:
    """Правка сводится к точке в конце — карточка ради неё бессмысленна.

    64 комментария за месяц отличались от стандарта только этим; каждый занимал
    отдельную карточку с кнопками. Такие документы уходят в пакетную операцию."""
    cur, new = (current or '').strip(), (proposed or '').strip()
    return bool(cur) and cur != new and new == cur + '.'


def needs_final_dot(text: str) -> bool:
    """Комментарий написан по стандарту, но без точки в конце.

    Формат «Имя: текст» обязателен: без него документу нужна настоящая правка
    («Яна готовая пенка» → «Яна: готовая пенка.»), а не механическая точка,
    иначе пачка допишет точку и оставит документ наполовину исправленным."""
    t = (text or '').strip()
    if not t or not _AUTHOR_RE.match(t):
        return False
    return not t.endswith(('.', '!', '?', ':', '…'))


_AUTHOR_RE = re.compile(r'^\s*([А-ЯЁ][а-яё]+)\s*:')


def _keep_author(current: str, proposed: str) -> str:
    """Вернуть подпись автора из исходного текста, если LLM подменила её другим именем.

    Правило 6 промпта запрещает менять имя, но модель нарушала его стабильно:
    в заказах маркетплейсов «Лена» превращалась в «Женя» по зоне ответственности.
    Откатываем только имя — остальная правка (регистр, точка, опечатки) остаётся."""
    was, now = _AUTHOR_RE.match(current or ''), _AUTHOR_RE.match(proposed or '')
    if not was or not now or was.group(1) == now.group(1):
        return proposed
    return proposed[:now.start(1)] + was.group(1) + proposed[now.end(1):]


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


async def collect_dot_fixes(days: int) -> list[dict]:
    """Документы, которым не хватает только точки в конце — для пакетной правки.

    Собирается кодом, без LLM: правило механическое, а спрашивать по одному
    документу за точку — пустая трата внимания владельца."""
    docs, _ = await collect_documents(days)
    out = []
    for d in docs:
        for field, entity, entity_id, text in (
            ('comment', d['entity'], d['id'], d['comment']),
            ('order_comment', 'customerorder', d.get('order_id'), d.get('order_comment')),
        ):
            if not entity_id or not needs_final_dot(text):
                continue
            out.append({'entity': entity, 'id': entity_id,
                        'label': d['label'] if field == 'comment'
                                 else f'Заказ покупателя №{d.get("order_name")}',
                        'comment': text, 'new_comment': text.strip() + '.'})
    # заказ, связанный с двумя отгрузками, не должен попасть в список дважды
    seen, unique = set(), []
    for item in out:
        key = (item['entity'], item['id'])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


async def apply_dot_fixes(items: list[dict]) -> int:
    """Записать точки пачкой; вернуть число обновлённых документов."""
    client = MoySkladAuditClient()
    done = 0
    async with new_session() as session:
        for item in items:
            try:
                await client.update_entity(session, item['entity'], item['id'],
                                           {'description': item['new_comment']})
                done += 1
            except Exception as e:
                logger.warning(f'[comments] точка не записалась в {item["label"]}: {e}')
    return done


async def apply_comment(entity: str, entity_id: str, new_comment: str):
    client = MoySkladAuditClient()
    async with new_session() as session:
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
