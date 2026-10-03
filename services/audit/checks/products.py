"""Проверки себестоимости товаров — порт из health check Horse Bio Insights.

Обе тяжёлые (отчёт остатков весит 5) и по природе не инкрементальные —
выполняются только в полном скане.
"""

import re
from collections import Counter
from datetime import datetime

from services.audit.context import AuditContext, is_free_supplier
from services.audit.specs import CheckSpec, RawFinding, Section, Severity

_DEVIATION_CRITICAL = 0.50    # >50% — похоже на ошибку единиц измерения (кг вместо г и т.п.)
_DEVIATION_IMPORTANT = 0.15   # 15–50% — рост цены/накладные, стоит взглянуть
_MAX_BATCH_REPORTS = 60       # запрос партий только по кандидатам, их единицы

# Маркеры бесплатного поступления в комментарии документа-источника: этикетки от
# типографии, образцы от технолога, упаковка «взял в ХБ». Для них ноль — норма,
# и решать это должен код: гонять LLM ради чтения слова «бесплатно» незачем.
# «нулев» одним корнем: в живых комментариях встречаются «нулевая стоимость»,
# «нулевой суммы», «по нулевым ценам» — падежи перечислять бессмысленно
_FREE_MARKERS = ('бесплатн', 'на пробу', 'подарок', 'подарен', 'нулев',
                 'даром', 'отдали', 'отдал')


def _previously_priced(sources: list[dict]) -> dict | None:
    """Самое свежее поступление товара с НЕнулевой ценой.

    Решающий факт: если такие же этикетки покупали по 36 ₽, «они бесплатные»
    больше не объяснение — либо цену забыли, либо в этот раз действительно отдали
    даром и это надо написать. Источники собраны по возрастанию даты."""
    priced = [s for s in sources if (s.get('price_kopecks') or 0) > 0]
    return priced[-1] if priced else None


def _explained_as_free(payload_docs: list[dict], last_supply: dict | None) -> bool:
    """Ноль объяснён: бесплатный поставщик или прямая оговорка в комментарии."""
    # ноль от бесплатного поставщика штатен в любом поступлении, не только
    # в последнем: приёмки старше окна сканирования в last_supply не попадают
    if last_supply and is_free_supplier(last_supply.get('agent')):
        return True
    if any(is_free_supplier(d.get('agent')) for d in payload_docs):
        return True
    comments = [(d.get('comment') or '') for d in payload_docs]
    if last_supply:
        comments.append(last_supply.get('comment') or '')
    comments = [c for c in comments if c.strip()]
    if not comments:
        return False
    return all(any(m in c.lower() for m in _FREE_MARKERS) for c in comments)


def _uom(row: dict) -> str:
    """Единица измерения товара из отчёта остатков: сырьё бывает в г, мл, л, шт —
    без неё LLM подставляет «кг» наугад и раздувает масштаб находки."""
    return ((row.get('uom') or {}).get('name') or '').strip()


def _payment_state(doc: dict) -> str:
    """«Оплачен ли документ» словами — цена, за которую заплатили ровно столько же,
    не может быть опечаткой ввода, и версию «лишний ноль» предлагать бессмысленно."""
    total, payed = doc.get('sum'), doc.get('payedSum')
    if total is None or payed is None:
        return 'оплата неизвестна'
    if payed >= total > 0:
        return f'оплачен полностью ({total / 100:,.2f} ₽)'.replace(',', ' ').replace('.', ',')
    if payed > 0:
        return (f'оплачен частично ({payed / 100:,.2f} из {total / 100:,.2f} ₽)'
                .replace(',', ' ').replace('.', ','))
    return 'не оплачен'


def _with_overhead(price: int | float, doc: dict) -> float:
    """Цена позиции + её доля накладных расходов приёмки.

    МойСклад распределяет накладные (доставку) на себестоимость позиций, поэтому
    FIFO товара законно выше цены в документе. Сравнивать FIFO с голой ценой —
    значит каждый раз находить расхождение, которого нет: у вазелина «отклонение
    52%» оказалось ровно долей доставки в приёмке.

    Распределение по цене (distribution='price') — доля пропорциональна сумме позиций.
    """
    overhead = (doc.get('overhead') or {}).get('sum', 0) or 0
    positions_sum = doc.get('sum', 0) or 0
    if overhead <= 0 or positions_sum <= 0:
        return float(price)   # копейки бывают дробными: вода стоит 2,5 коп/г
    return price * (1 + overhead / positions_sum)


async def _last_supply_prices(ctx: AuditContext) -> dict[str, dict]:
    """href товара -> цена из самой свежей приёмки за окно сканирования."""
    docs = await ctx.cached_list(
        'supply_full_window', 'supply',
        filters=f'moment>={ctx.scan_since_moment};applicable=true',
        expand='positions.assortment,agent',
        order='moment,desc',
    )
    out: dict[str, dict] = {}
    for d in docs:   # порядок moment,desc — первое вхождение товара и есть последняя приёмка
        for p in d.get('positions', {}).get('rows', []):
            a = p.get('assortment', {})
            href = a.get('meta', {}).get('href', '').split('?')[0]
            if href and href not in out:
                out[href] = {
                    'agent': ((d.get('agent') or {}).get('name')
                              if isinstance(d.get('agent'), dict) else None),
                    'price_kopecks': p.get('price', 0),
                    # с накладными: именно эту величину и показывает FIFO
                    'cost_with_overhead_kopecks': _with_overhead(p.get('price', 0), d),
                    'overhead_kopecks': (d.get('overhead') or {}).get('sum', 0) or 0,
                    'supply': d.get('name'),
                    'moment': (d.get('moment') or '')[:10],
                    'comment': (d.get('description') or '')[:200],
                    'payment': _payment_state(d),
                }
    return out


async def _product_source_docs(
    ctx: AuditContext,
) -> tuple[dict[str, list], dict[str, str], dict[str, str]]:
    """(href товара -> документы-источники с ценами и комментариями,
    href товара -> uuidHref для UI-ссылки,
    название документа-источника -> uuidHref — ссылки держим отдельно от фактов,
    чтобы не гонять их через LLM).

    Комментарий источника — ключ к вердикту: «Имя: тара, которую отдали
    бесплатно» делает нулевую цену нормой, а не ошибкой.
    Берём ВСЮ историю: источник нулевого FIFO часто старше окна сканирования
    (кейс флакона: оприходование от марта при окне с апреля)."""
    sources: dict[str, list] = {}
    ui_links: dict[str, str] = {}
    doc_links: dict[str, str] = {}
    for entity, label in (('supply', 'Приёмка'), ('enter', 'Оприходование')):
        docs = await ctx.cached_list(
            f'{entity}_all_history', entity,
            expand='positions.assortment,agent',
            order='moment,asc',
            max_rows=2000,
        )
        for d in docs:
            for p in d.get('positions', {}).get('rows', []):
                a = p.get('assortment', {})
                href = a.get('meta', {}).get('href', '').split('?')[0]
                if not href:
                    continue
                if a.get('meta', {}).get('uuidHref'):
                    ui_links[href] = a['meta']['uuidHref']
                source = {
                    'doc': f'{label} №{d.get("name")} от {(d.get("moment") or "")[:10]}',
                    'moment': (d.get('moment') or '')[:19],
                    'agent': ((d.get('agent') or {}).get('name')
                              if isinstance(d.get('agent'), dict) else None),
                    'quantity': p.get('quantity'),
                    'price_kopecks': p.get('price', 0),
                    'comment': (d.get('description') or '')[:200],
                }
                doc_links[source['doc']] = (d.get('meta') or {}).get('uuidHref', '')
                if entity == 'supply':   # у оприходования оплаты не бывает
                    source['payment'] = _payment_state(d)
                sources.setdefault(href, []).append(source)
    return sources, ui_links, doc_links


def previous_receipt_price(sources: list[dict], before_moment: str) -> dict | None:
    """Последнее поступление товара с ценой СТРОГО раньше указанного момента.

    Оприходование фиксирует цену конкретной покупки, а FIFO — среднюю по остатку:
    стрейч-плёнку брали по 498, 720 и 733 ₽ при средней 576 ₽, и сравнение со
    средней объявляло ошибкой обычный разброс закупочных цен."""
    earlier = [s for s in sources
               if (s.get('price_kopecks') or 0) > 0
               and (s.get('moment') or '') < before_moment]
    return earlier[-1] if earlier else None


class FifoZeroCheck(CheckSpec):
    """Товар с остатком, но нулевой себестоимостью FIFO.

    Такой товар входит в готовую продукцию «бесплатно» и занижает её
    себестоимость. Для бесплатных этикеток ноль может быть нормой —
    это решает LLM-аналитик по названию и последней приёмке."""

    id = 'fifo_zero'
    section = Section.PRODUCTS
    title = 'Себестоимость 0 у товара с остатком'
    default_severity = Severity.CRITICAL
    supports_incremental = False

    async def detect(self, ctx: AuditContext, since: datetime | None) -> list[RawFinding]:
        rows = await ctx.client.stock_all(ctx.session, stock_mode='all')
        last = await _last_supply_prices(ctx)
        sources, ui_links, doc_links = await _product_source_docs(ctx)
        out = []
        for r in rows:
            if r.get('stock', 0) <= 0 or r.get('price', 0) != 0:
                continue
            href = r.get('meta', {}).get('href', '').split('?')[0]
            if _explained_as_free(sources.get(href, []), last.get(href)):
                continue   # «получено бесплатно» написано в документе — ноль корректен
            # чинить владелец будет документ, внёсший ноль, а не карточку товара —
            # ссылка ведёт туда (находка по-прежнему одна на товар)
            zero_doc = next((s for s in reversed(sources.get(href, []))
                             if not s.get('price_kopecks')), None)
            out.append(RawFinding(
                entity_type='product',
                entity_id=href.split('/')[-1],
                entity_href=href,
                entity_name=(f'{zero_doc["doc"]} · {r.get("name", "?")}' if zero_doc
                             else r.get('name', '?')),
                severity=self.default_severity,
                payload={
                    'product': r.get('name'),
                    'folder': (r.get('folder') or {}).get('name', ''),
                    'stock': r.get('stock'),
                    'uom': _uom(r),
                    'fifo_kopecks': 0,
                    'last_supply': last.get(href),
                    'previously_priced': _previously_priced(sources.get(href, [])),
                    'source_documents': sources.get(href, [])[:6],
                    'note': ('Нулевая себестоимость занижает FIFO готовой продукции, '
                             'в которую входит этот товар. ЧИТАЙ комментарии '
                             'документов-источников: «отдали бесплатно», «подарок» '
                             'и т.п. делают ноль нормой для любого товара. '
                             'Поле previously_priced — поступление ЭТОГО ЖЕ товара '
                             'с ненулевой ценой: если оно заполнено, товар покупали '
                             'за деньги, и ноль требует объяснения в комментарии; '
                             'если пусто — товар никогда не стоил денег.'),
                },
                fingerprint_salt='',
                ui_link=(doc_links.get(zero_doc['doc']) if zero_doc else None)
                        or ui_links.get(href, ''),
            ))
        return out

    def explain(self, payload: dict) -> str:
        return (f'Остаток {payload.get("stock")} {payload.get("uom", "")}'.rstrip() +
                ', себестоимость 0 ₽. '
                'Всё, что производится из этого товара, получит заниженную себестоимость.')


class RootProductCheck(CheckSpec):
    """Товар лежит в корне справочника, без папки.

    Папка задаёт тип товара (сырьё, тара, этикетки, готовая продукция) и схему
    кодов: 1-xxx сырьё, 2-xxx продукция, 3-xxx этикетки, 4-xxx тара, 5-xxx хозтовары.
    Товар без папки выпадает из отчётов по группам, и код ему обычно тоже
    не присваивают — живой кейс: «Пробники тары» с кодом 0."""

    id = 'product_without_folder'
    section = Section.PRODUCTS
    title = 'Товар заведён без папки'
    default_severity = Severity.WARNING
    supports_incremental = False

    async def detect(self, ctx: AuditContext, since: datetime | None) -> list[RawFinding]:
        products = await ctx.cached_list(
            'products_all', 'product', order='updated,desc', max_rows=2000)
        folders = {f['id']: f for f in await ctx.cached_list(
            'productfolders_all', 'productfolder', max_rows=500)}
        # какие коды в ходу у каждой папки — чтобы подсказать правильный префикс
        prefixes: dict[str, set] = {}
        for p in products:
            folder = p.get('productFolder')
            code = (p.get('code') or '').strip()
            if folder and '-' in code:
                fid = folder['meta']['href'].split('/')[-1]
                prefixes.setdefault(fid, set()).add(code.split('-')[0])

        out = []
        for p in products:
            if p.get('productFolder') or p.get('archived'):
                continue
            out.append(RawFinding(
                entity_type='product',
                entity_id=p['id'],
                entity_href=p['meta']['href'],
                entity_name=p.get('name', '?'),
                severity=self.default_severity,
                payload={
                    'product': p.get('name'),
                    'code': (p.get('code') or '').strip() or None,
                    'article': p.get('article'),
                    'uom': ((p.get('uom') or {}).get('name')),
                    'description': (p.get('description') or '')[:200],
                    'folders_available': sorted(
                        f"{f['name']} (коды {'/'.join(sorted(prefixes.get(fid, {'—'})))}-xxx)"
                        for fid, f in folders.items() if prefixes.get(fid)),
                    'note': ('Товар не отнесён ни к одной папке справочника. Папка задаёт '
                             'тип товара и схему кодов; без неё товар выпадает из отчётов '
                             'по группам. Предложи подходящую папку из folders_available '
                             'по смыслу названия и код с её префиксом.'),
                },
                fingerprint_salt='',
                ui_link=(p.get('meta') or {}).get('uuidHref', ''),
            ))
        return out

    def explain(self, payload: dict) -> str:
        code = payload.get('code')
        return (f'Товар «{payload.get("product")}» лежит в корне справочника'
                + (f' с кодом {code}' if code else ' без кода')
                + '. Нужно положить в папку по типу товара и присвоить код по её схеме.')


def _folder_full_path(folder: dict) -> str:
    path = folder.get('pathName') or ''
    return f'{path}/{folder["name"]}' if path else folder['name']


class ProductInNonLeafFolderCheck(CheckSpec):
    """Товар лежит в папке-разделе, у которой есть подпапки, а не в конечной.

    Структура справочника — раздел → тип товара (напр. «Готовая продукция» →
    «Шампунь для собак»); товары должны лежать только в конечных папках.
    Товар в разделе теряется из отчётов по типу так же, как товар без папки
    вовсе (product_without_folder) — просто на уровень выше."""

    id = 'product_in_nonleaf_folder'
    section = Section.PRODUCTS
    title = 'Товар лежит в разделе, а не в конечной папке'
    default_severity = Severity.WARNING
    supports_incremental = False
    llm_triage = False   # структура папок — факт, не вопрос суждения

    async def detect(self, ctx: AuditContext, since: datetime | None) -> list[RawFinding]:
        products = await ctx.cached_list(
            'products_all_folder', 'product', expand='productFolder', order='name', max_rows=3000)
        folders = await ctx.cached_list('productfolders_all', 'productfolder', max_rows=500)
        parent_paths = {f['pathName'] for f in folders if f.get('pathName')}
        nonleaf_ids = {f['id'] for f in folders if _folder_full_path(f) in parent_paths}

        out = []
        for p in products:
            if p.get('archived'):
                continue
            folder = p.get('productFolder')
            if not folder or folder['meta']['href'].split('/')[-1] not in nonleaf_ids:
                continue
            out.append(RawFinding(
                entity_type='product',
                entity_id=p['id'],
                entity_href=p['meta']['href'],
                entity_name=p.get('name', '?'),
                severity=self.default_severity,
                payload={'product': p.get('name'), 'folder': folder.get('name'),
                         'code': (p.get('code') or '').strip() or None},
                fingerprint_salt='',
                ui_link=(p.get('meta') or {}).get('uuidHref', ''),
            ))
        return out

    def explain(self, payload: dict) -> str:
        return (f'Товар «{payload.get("product")}» лежит прямо в разделе '
                f'«{payload.get("folder")}», у которого есть подпапки-типы. '
                f'Нужно переложить в конечную подпапку по типу товара.')


class ProductCodeSchemeCheck(CheckSpec):
    """Код товара не совпадает с префиксом, принятым в его папке.

    Схема компании: у каждой конечной папки — свой префикс кода (1 сырьё,
    2 продукция, 3 этикетки, 4 тара, 5 хозтовары). Живой кейс: флакон и
    крышка попали в «Тару» с кодами 3-143/3-144 — префиксом этикеток вместо
    4-xxx. Большинство кодов папки задаёт «правильный» префикс, дырки в
    нумерации при этом не проверяем — товар мог быть архивирован/удалён,
    и дырка тогда норма, а не сигнал."""

    id = 'product_code_scheme'
    section = Section.PRODUCTS
    title = 'Код товара не по схеме папки'
    default_severity = Severity.WARNING
    supports_incremental = False
    llm_triage = False   # схема кодов — факт большинства, не вопрос суждения

    _MIN_SAMPLES = 3   # меньше — не набралась статистика, молчим

    async def detect(self, ctx: AuditContext, since: datetime | None) -> list[RawFinding]:
        products = await ctx.cached_list(
            'products_all_folder', 'product', expand='productFolder', order='name', max_rows=3000)
        by_folder: dict[str, list[dict]] = {}
        for p in products:
            if p.get('archived'):
                continue
            folder = p.get('productFolder')
            if not folder:
                continue
            fid = folder['meta']['href'].split('/')[-1]
            by_folder.setdefault(fid, []).append(p)

        # дубли кодов по всему справочнику — сигнал сам по себе, папка ни при чём
        code_owners: dict[str, list[dict]] = {}
        for p in products:
            if p.get('archived'):
                continue
            code = (p.get('code') or '').strip()
            if code:
                code_owners.setdefault(code, []).append(p)

        out = []
        seen_dup_codes = set()
        for items in by_folder.values():
            prefixes = Counter()
            for p in items:
                code = (p.get('code') or '').strip()
                if '-' in code:
                    prefixes[code.split('-')[0]] += 1
            folder_name = items[0]['productFolder'].get('name', '?')
            has_majority = (prefixes and sum(prefixes.values()) >= self._MIN_SAMPLES
                            and prefixes.most_common(1)[0][1]
                                > sum(prefixes.values()) - prefixes.most_common(1)[0][1])
            main_prefix = prefixes.most_common(1)[0][0] if has_majority else None

            for p in items:
                code = (p.get('code') or '').strip()
                dupes = code_owners.get(code, [])
                if code and len(dupes) > 1 and code not in seen_dup_codes:
                    seen_dup_codes.add(code)
                    out.append(RawFinding(
                        entity_type='product',
                        entity_id=p['id'],
                        entity_href=p['meta']['href'],
                        entity_name=f'{p.get("name", "?")} — дубль кода {code}',
                        severity=Severity.WARNING,
                        payload={'issue': 'duplicate_code', 'code': code,
                                 'products': [d.get('name') for d in dupes]},
                        fingerprint_salt=f'dup:{code}',
                        ui_link=(p.get('meta') or {}).get('uuidHref', ''),
                    ))
                    continue
                if main_prefix is None:
                    continue
                prefix = code.split('-')[0] if '-' in code else None
                if prefix is None or prefix == main_prefix:
                    continue
                suffix = code.split('-', 1)[1] if '-' in code else None
                out.append(RawFinding(
                    entity_type='product',
                    entity_id=p['id'],
                    entity_href=p['meta']['href'],
                    entity_name=p.get('name', '?'),
                    severity=self.default_severity,
                    payload={
                        'issue': 'wrong_prefix',
                        'product': p.get('name'),
                        'code': code,
                        'folder': folder_name,
                        'expected_prefix': main_prefix,
                        'suggested_code': (f'{main_prefix}-{suffix}'
                                           if suffix and suffix.isdigit() else None),
                    },
                    fingerprint_salt=code,
                    ui_link=(p.get('meta') or {}).get('uuidHref', ''),
                ))
        return out

    def explain(self, payload: dict) -> str:
        if payload.get('issue') == 'duplicate_code':
            names = ', '.join(f'«{n}»' for n in payload.get('products', []))
            return f'Код {payload.get("code")} присвоен нескольким товарам: {names}.'
        tail = (f' По схеме папки должно быть {payload["suggested_code"]}.'
                if payload.get('suggested_code') else '')
        return (f'Товар «{payload.get("product")}» лежит в папке «{payload.get("folder")}», '
                f'где у остальных товаров код начинается на {payload.get("expected_prefix")}-, '
                f'а у него {payload.get("code")}.{tail}')


_SIZE_RE = re.compile(r'(\d+)\s*(мл|г|л)\b', re.IGNORECASE)
_FAMILY_TRIM_RE = re.compile(r'\s*\d+\s*(?:мл|г|л)\.?\s*(?:\([^)]*\))?\s*$', re.IGNORECASE)
_ARTICLE_RE = re.compile(r'^(\d{3})\.(\d{3})\.(\d{2})$')


def _extract_size(name: str) -> tuple[int, str] | None:
    """Последнее число с единицей измерения в названии — это фасовка."""
    matches = _SIZE_RE.findall(name)
    if not matches:
        return None
    value, unit = matches[-1]
    return int(value), unit.lower()


def _family_name(name: str) -> str:
    """Название товара без фасовки и цветовой пометки — для сравнения фасовок одного продукта."""
    return _FAMILY_TRIM_RE.sub('', name).strip().lower()


class ProductArticleSchemeCheck(CheckSpec):
    """Артикул готовой продукции не по схеме GGG.NNN.VV.

    GGG — группа товара (100 шампунь, 200 кондиционер, 300 репеллент,
    400 амуниция), NNN — номер продукта внутри группы, VV — код фасовки.
    VV = фасовка / делитель, но делитель РАЗНЫЙ по группам (подтверждено
    реестром штрихкодов ДиСАИ и живыми данными): у шампуня/кондиционера/
    репеллента делитель 100 (500 мл → 05, 5000 мл → 50, 300 мл → 03), а у
    амуниции — 10 (200 мл → 20, 250 г → 25). Единой формулы нет, поэтому
    делитель вычисляется по большинству пар «фасовка/VV» внутри каждой
    группы, а не зашивается числом.

    У одного продукта в разных фасовках NNN должен совпадать — меняется
    только VV (живой кейс: Кока-Кола 500/5000 мл — 200.030.05/.50, совпадает;
    а у Персик-овёс 500/5000 мл — 200.041.05/200.043.05, разъехался и NNN,
    и VV). «Основы» (полуфабрикаты) и пробники артикула не имеют по
    определению — это норма, не находка."""

    id = 'product_article_scheme'
    section = Section.PRODUCTS
    title = 'Артикул товара не по схеме'
    default_severity = Severity.WARNING
    supports_incremental = False
    llm_triage = False   # схема артикула — факт из названия и реестра, не суждение

    _EXEMPT_MARKERS = ('основа', 'пробник')
    _MIN_DIVISOR_SAMPLES = 3   # меньше — не набралась статистика на делитель, молчим

    async def detect(self, ctx: AuditContext, since: datetime | None) -> list[RawFinding]:
        products = await ctx.cached_list(
            'products_all_folder', 'product', expand='productFolder', order='name', max_rows=3000)
        finished = []
        for p in products:
            if p.get('archived'):
                continue
            folder = p.get('productFolder') or {}
            in_finished_tree = (folder.get('name') == 'Готовая продукция'
                                or folder.get('pathName') == 'Готовая продукция')
            if not in_finished_tree:
                continue
            name_low = (p.get('name') or '').lower()
            if any(m in name_low for m in self._EXEMPT_MARKERS):
                continue
            finished.append(p)

        issues: dict[str, list[dict]] = {}   # product id -> issue dicts
        parsed: dict[str, tuple] = {}         # product id -> (ggg, nnn, vv)
        article_owners: dict[str, list[dict]] = {}
        sizes: dict[str, tuple[int, str]] = {}   # product id -> (value, unit)

        for p in finished:
            article = (p.get('article') or '').strip()
            if article:
                article_owners.setdefault(article, []).append(p)
            size = _extract_size(p.get('name') or '')
            if size:
                sizes[p['id']] = size
            if not article:
                issues.setdefault(p['id'], []).append({'kind': 'missing'})
                continue
            m = _ARTICLE_RE.match(article)
            if not m:
                issues.setdefault(p['id'], []).append({'kind': 'malformed', 'article': article})
                continue
            parsed[p['id']] = m.groups()

        # делитель размера — по большинству пар (фасовка, VV) внутри группы GGG,
        # а не зашитым числом: у амуниции он другой, чем у шампуня/кондиционера
        divisor_votes: dict[str, Counter] = {}
        for pid, (ggg, _nnn, vv) in parsed.items():
            size = sizes.get(pid)
            vv_int = int(vv)
            if not size or vv_int == 0 or size[0] % vv_int:
                continue
            divisor_votes.setdefault(ggg, Counter())[size[0] // vv_int] += 1
        group_divisor = {ggg: c.most_common(1)[0][0]
                         for ggg, c in divisor_votes.items()
                         if sum(c.values()) >= self._MIN_DIVISOR_SAMPLES}

        def _expected_vv(ggg: str, size: tuple[int, str] | None) -> str | None:
            divisor = group_divisor.get(ggg)
            if not divisor or not size or size[0] % divisor:
                return None
            vv_int = size[0] // divisor
            return f'{vv_int:02d}' if 0 < vv_int < 100 else None

        for pid, (ggg, nnn, vv) in parsed.items():
            expected_vv = _expected_vv(ggg, sizes.get(pid))
            if expected_vv is None or expected_vv == vv:
                continue
            size = sizes[pid]
            issues.setdefault(pid, []).append({
                'kind': 'suffix_mismatch', 'article': f'{ggg}.{nnn}.{vv}',
                'size': f'{size[0]} {size[1]}', 'expected_vv': expected_vv,
                'suggested_article': f'{ggg}.{nnn}.{expected_vv}',
            })

        # дубли артикулов
        for article, owners in article_owners.items():
            if len(owners) > 1:
                for p in owners:
                    issues.setdefault(p['id'], []).append({
                        'kind': 'duplicate', 'article': article,
                        'products': [o.get('name') for o in owners],
                    })

        # NNN должен совпадать у фасовок одного продукта
        families: dict[tuple[str, str], list[str]] = {}   # (ggg, family) -> [product_id,...]
        for pid, (ggg, _nnn, _vv) in parsed.items():
            families.setdefault((ggg, _family_name(next(
                p for p in finished if p['id'] == pid).get('name') or '')), []).append(pid)
        by_id = {p['id']: p for p in finished}
        for (ggg, _fam), pids in families.items():
            nnns = {parsed[pid][1] for pid in pids}
            if len(nnns) <= 1:
                continue
            # эталон — фасовка с наименьшим объёмом: по живым кейсам именно она
            # регистрируется первой (штрихкод/реестр), остальные подстраиваются
            canonical_pid = min(pids, key=lambda pid: sizes.get(pid) or (10 ** 9, ''))
            canonical_nnn = parsed[canonical_pid][1]
            siblings = [by_id[pid].get('name') for pid in pids]
            for pid in pids:
                if parsed[pid][1] == canonical_nnn:
                    continue
                ggg_, _nnn, vv_ = parsed[pid]
                # если для этой фасовки уже известен правильный VV — используем его,
                # а не старый (он мог быть неверным вместе с NNN, живой кейс:
                # Персик-овёс 5000 мл: 200.043.05 → должно быть 200.041.50)
                fixed_vv = _expected_vv(ggg_, sizes.get(pid)) or vv_
                issues.setdefault(pid, []).append({
                    'kind': 'nnn_mismatch', 'article': f'{ggg_}.{_nnn}.{vv_}',
                    'expected_nnn': canonical_nnn,
                    'suggested_article': f'{ggg_}.{canonical_nnn}.{fixed_vv}',
                    'siblings': siblings,
                })

        out = []
        for pid, item_issues in issues.items():
            p = by_id.get(pid) or next(x for x in finished if x['id'] == pid)
            out.append(RawFinding(
                entity_type='product',
                entity_id=p['id'],
                entity_href=p['meta']['href'],
                entity_name=p.get('name', '?'),
                severity=self.default_severity,
                payload={'product': p.get('name'), 'article': (p.get('article') or '').strip(),
                         'issues': item_issues},
                fingerprint_salt=','.join(sorted(i['kind'] for i in item_issues)),
                ui_link=(p.get('meta') or {}).get('uuidHref', ''),
            ))
        return out

    def explain(self, payload: dict) -> str:
        parts = []
        for i in payload.get('issues', []):
            kind = i['kind']
            if kind == 'missing':
                parts.append('у готового товара нет артикула')
            elif kind == 'malformed':
                parts.append(f'артикул «{i["article"]}» не в формате GGG.NNN.VV')
            elif kind == 'suffix_mismatch':
                parts.append(f'артикул «{i["article"]}» — фасовка {i["size"]}, '
                             f'суффикс должен быть {i["expected_vv"]} '
                             f'(предлагаемый артикул {i["suggested_article"]})')
            elif kind == 'duplicate':
                names = ', '.join(f'«{n}»' for n in i.get('products', []))
                parts.append(f'артикул «{i["article"]}» присвоен нескольким товарам: {names}')
            elif kind == 'nnn_mismatch':
                sib = ', '.join(f'«{n}»' for n in i.get('siblings', []))
                parts.append(f'номер продукта в артикуле «{i["article"]}» не совпадает с '
                             f'фасовками того же товара ({sib}); '
                             f'предлагаемый артикул {i["suggested_article"]}')
        return f'Товар «{payload.get("product")}»: ' + '; '.join(parts) + '.'


class FifoDeviationCheck(CheckSpec):
    """Текущий FIFO товара заметно расходится с ценой его последней приёмки.

    >50% — почти всегда ошибка (перепутаны единицы измерения, лишний ноль);
    15–50% — рост цены или недооценённые накладные, стоит взглянуть."""

    id = 'fifo_vs_last_supply'
    section = Section.PRODUCTS
    title = 'Себестоимость расходится с последней приёмкой'
    supports_incremental = False

    async def detect(self, ctx: AuditContext, since: datetime | None) -> list[RawFinding]:
        rows = await ctx.client.stock_all(ctx.session, stock_mode='all')
        last = await _last_supply_prices(ctx)
        sources, ui_links, _ = await _product_source_docs(ctx)
        out = []
        batches_budget = _MAX_BATCH_REPORTS
        for r in rows:
            if r.get('stock', 0) <= 0:
                continue
            fifo = r.get('price', 0)
            if fifo <= 0:
                continue   # нули ловит fifo_zero
            href = r.get('meta', {}).get('href', '').split('?')[0]
            ls = last.get(href)
            if not ls or ls['price_kopecks'] <= 0:
                continue
            # сравниваем с ценой ПЛЮС накладные: FIFO их уже включает, и без этого
            # доля доставки читается как «расхождение себестоимости»
            baseline = ls.get('cost_with_overhead_kopecks') or ls['price_kopecks']
            deviation = abs(fifo - baseline) / baseline
            if deviation < _DEVIATION_IMPORTANT:
                continue
            # остаток из нескольких партий: FIFO — их средневзвешенная, и отличие
            # от цены последней приёмки ничего не значит (живой кейс лауроилглутамата:
            # 4999 г по 0,53 ₽ и 1000 г по 2,20 ₽ дают ровно те 0,81 ₽, что в отчёте)
            batches = []
            if batches_budget > 0:
                try:
                    batches = await ctx.client.stock_batches(ctx.session, href.split('/')[-1])
                    batches_budget -= 1
                except Exception:
                    batches = []
            if len(batches) > 1:
                continue

            severity = (Severity.CRITICAL if deviation >= _DEVIATION_CRITICAL
                        else Severity.IMPORTANT)
            out.append(RawFinding(
                entity_type='product',
                entity_id=href.split('/')[-1],
                entity_href=href,
                entity_name=r.get('name', '?'),
                severity=severity,
                payload={
                    'product': r.get('name'),
                    'folder': (r.get('folder') or {}).get('name', ''),
                    'stock': r.get('stock'),
                    'uom': _uom(r),
                    'fifo_kopecks': fifo,
                    'stock_value_kopecks': round(fifo * r.get('stock', 0)),
                    'last_supply': ls,
                    'compared_with_kopecks': baseline,
                    'stock_batches': len(batches),
                    'deviation_percent': round(deviation * 100, 1),
                    'source_documents': sources.get(href, [])[:6],
                    'note': ('Отклонение уже посчитано ОТ ЦЕНЫ С НАКЛАДНЫМИ '
                             '(compared_with_kopecks): доля доставки в себестоимость '
                             'входит, объяснять расхождение накладными расходами больше '
                             'нельзя. Отклонение >50% обычно означает ошибку в приёмке '
                             '(единицы измерения, лишний ноль); 15–50% — рост цены '
                             'или старые партии на складе. ЧИТАЙ комментарии документов-'
                             'источников: бесплатные партии в истории объясняют '
                             'заниженный FIFO без чьей-либо ошибки. Поле payment '
                             'у источника — проверка версии «опечатка в цене»: '
                             'если документ оплачен полностью, сумма подтверждена '
                             'деньгами и версию про лишний ноль НЕ предлагай, '
                             'разница цен — вопрос закупки, а не учёта. '
                             'stock_value — цена вопроса, соразмеряй с ней выводы.'),
                },
                # новая приёмка = новая точка сравнения = новый сигнал
                fingerprint_salt=str(ls['supply']),
                ui_link=ui_links.get(href, ''),
            ))
        return out

    def explain(self, payload: dict) -> str:
        ls = payload.get('last_supply') or {}
        per = f'/{payload["uom"]}' if payload.get('uom') else ''
        return (f'FIFO {payload.get("fifo_kopecks", 0) / 100:,.2f} ₽{per} против '
                f'{ls.get("price_kopecks", 0) / 100:,.2f} ₽{per} в приёмке '
                f'№{ls.get("supply")} от {ls.get("moment")} — отклонение '
                f'{payload.get("deviation_percent")}%.').replace(',', ' ')
