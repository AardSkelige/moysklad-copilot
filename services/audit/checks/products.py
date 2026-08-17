"""Проверки себестоимости товаров — порт из health check Horse Bio Insights.

Обе тяжёлые (отчёт остатков весит 5) и по природе не инкрементальные —
выполняются только в полном скане.
"""

from datetime import datetime

from services.audit.context import AuditContext
from services.audit.specs import CheckSpec, RawFinding, Section, Severity

_DEVIATION_CRITICAL = 0.50    # >50% — похоже на ошибку единиц измерения (кг вместо г и т.п.)
_DEVIATION_IMPORTANT = 0.15   # 15–50% — рост цены/накладные, стоит взглянуть


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


async def _last_supply_prices(ctx: AuditContext) -> dict[str, dict]:
    """href товара -> цена из самой свежей приёмки за окно сканирования."""
    docs = await ctx.cached_list(
        'supply_full_window', 'supply',
        filters=f'moment>={ctx.scan_since_moment};applicable=true',
        expand='positions.assortment',
        order='moment,desc',
    )
    out: dict[str, dict] = {}
    for d in docs:   # порядок moment,desc — первое вхождение товара и есть последняя приёмка
        for p in d.get('positions', {}).get('rows', []):
            a = p.get('assortment', {})
            href = a.get('meta', {}).get('href', '').split('?')[0]
            if href and href not in out:
                out[href] = {
                    'price_kopecks': p.get('price', 0),
                    'supply': d.get('name'),
                    'moment': (d.get('moment') or '')[:10],
                    'comment': (d.get('description') or '')[:200],
                    'payment': _payment_state(d),
                }
    return out


async def _product_source_docs(ctx: AuditContext) -> tuple[dict[str, list], dict[str, str]]:
    """(href товара -> документы-источники с ценами и комментариями,
    href товара -> uuidHref для UI-ссылки).

    Комментарий источника — ключ к вердикту: «Имя: тара, которую отдали
    бесплатно» делает нулевую цену нормой, а не ошибкой.
    Берём ВСЮ историю: источник нулевого FIFO часто старше окна сканирования
    (кейс флакона: оприходование от марта при окне с апреля)."""
    sources: dict[str, list] = {}
    ui_links: dict[str, str] = {}
    for entity, label in (('supply', 'Приёмка'), ('enter', 'Оприходование')):
        docs = await ctx.cached_list(
            f'{entity}_all_history', entity,
            expand='positions.assortment',
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
                    'quantity': p.get('quantity'),
                    'price_kopecks': p.get('price', 0),
                    'comment': (d.get('description') or '')[:200],
                }
                if entity == 'supply':   # у оприходования оплаты не бывает
                    source['payment'] = _payment_state(d)
                sources.setdefault(href, []).append(source)
    return sources, ui_links


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
        sources, ui_links = await _product_source_docs(ctx)
        out = []
        for r in rows:
            if r.get('stock', 0) <= 0 or r.get('price', 0) != 0:
                continue
            href = r.get('meta', {}).get('href', '').split('?')[0]
            out.append(RawFinding(
                entity_type='product',
                entity_id=href.split('/')[-1],
                entity_href=href,
                entity_name=r.get('name', '?'),
                severity=self.default_severity,
                payload={
                    'product': r.get('name'),
                    'folder': (r.get('folder') or {}).get('name', ''),
                    'stock': r.get('stock'),
                    'uom': _uom(r),
                    'fifo_kopecks': 0,
                    'last_supply': last.get(href),
                    'source_documents': sources.get(href, [])[:6],
                    'note': ('Нулевая себестоимость занижает FIFO готовой продукции, '
                             'в которую входит этот товар. ЧИТАЙ комментарии '
                             'документов-источников: «отдали бесплатно», «подарок» '
                             'и т.п. делают ноль нормой для любого товара.'),
                },
                fingerprint_salt='',
                ui_link=ui_links.get(href, ''),
            ))
        return out

    def explain(self, payload: dict) -> str:
        return (f'Остаток {payload.get("stock")} {payload.get("uom", "")}'.rstrip() +
                ', себестоимость 0 ₽. '
                'Всё, что производится из этого товара, получит заниженную себестоимость.')


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
        sources, ui_links = await _product_source_docs(ctx)
        out = []
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
            deviation = abs(fifo - ls['price_kopecks']) / ls['price_kopecks']
            if deviation < _DEVIATION_IMPORTANT:
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
                    'deviation_percent': round(deviation * 100, 1),
                    'source_documents': sources.get(href, [])[:6],
                    'note': ('Отклонение >50% обычно означает ошибку в приёмке '
                             '(единицы измерения, лишний ноль); 15–50% — рост цены '
                             'или накладные расходы. ЧИТАЙ комментарии документов-'
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
