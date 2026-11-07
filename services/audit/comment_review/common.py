import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from core import config
from core.logger import logger
from integrations.moysklad_audit import MoySkladAuditClient
from services.audit.team_context import (
    AUTHOR_FINANCE,
    AUTHOR_MARKETPLACE,
    AUTHOR_PURCHASES,
    AUTHOR_SALES,
    CANONICAL_NAMES,
    MARKETPLACES,
)
from services.llm_service import LLMClient
from integrations.moysklad_base import new_session


_BATCH_SIZE = 8
# пачки уходят в модель по нескольку сразу: строго по очереди 30 дней
# оценивались почти 4 минуты, а пачки друг от друга не зависят
_PARALLEL_BATCHES = 3

# progress(сделано, всего) — чтобы владелец видел, что работа идёт
Progress = Callable[[int, int], Awaitable[None]] | None


async def _ask_batches(llm: LLMClient, system: str, payloads: list[list],
                       tag: str, progress: Progress = None) -> list[list | None]:
    """Отправить пачки в модель параллельно; вердикты вернуть в порядке пачек.

    Разбор ответа — строго по порядку исходных пачек: от него зависят правила
    вроде «заказ с двумя отгрузками правит только первая карточка».
    None на месте пачки = модель ответила неразборчиво, пачку пропускаем."""
    semaphore = asyncio.Semaphore(_PARALLEL_BATCHES)
    done = 0

    async def one(i: int, payload: list) -> list | None:
        nonlocal done
        async with semaphore:
            try:
                resp = await llm.chat([
                    {'role': 'system', 'content': system},
                    {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)},
                ])
                content = (resp.get('content') or '').strip()
                arr_start, arr_end = content.find('['), content.rfind(']')
                verdicts = json.loads(content[arr_start:arr_end + 1])
            except Exception as e:
                logger.warning(f'[{tag}] батч {i * _BATCH_SIZE} не разобрался: {e}')
                verdicts = None
        done += 1
        if progress:
            await progress(done, len(payloads))
        return verdicts

    return await asyncio.gather(*(one(i, p) for i, p in enumerate(payloads)))


def _batches(docs: list[dict]) -> list[list[dict]]:
    return [docs[i:i + _BATCH_SIZE] for i in range(0, len(docs), _BATCH_SIZE)]

# стандартный комментарий отгрузки маркетплейсу с нулевыми накладными —
# используется и в промпте LLM, и в пакетной кнопке (единственное место истины)
DEMAND_MARKETPLACE_ZERO_COMMENT = 'Накладные расходы 0 — доставку оплачивает маркетплейс.'

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


def _too_fresh(moment: str | None) -> bool:
    """Отгрузке ещё не исполнилось суток — рано проверять комментарий."""
    try:
        doc_moment = datetime.strptime((moment or '')[:19], '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return False
    return doc_moment > datetime.now() - timedelta(hours=config.AUDIT_DEMAND_GRACE_HOURS)


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
    async with new_session() as session:
        await client.update_entity(session, entity, entity_id,
                                   {'description': new_comment})
