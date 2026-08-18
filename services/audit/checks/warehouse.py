"""Проверки раздела «Склад»: оприходования против FIFO и отрицательные остатки."""

from datetime import datetime

from services.audit.checks.products import _product_source_docs, previous_receipt_price
from services.audit.context import AuditContext, format_moment
from services.audit.specs import CheckSpec, RawFinding, Section, Severity

# Хозтовары и упаковку покупают в разных магазинах, и цена гуляет: стрейч-плёнка
# бралась по 498 и 733 ₽. Ниже 30% — обычный разброс закупки, а не ошибка учёта.
_FIFO_DEVIATION_THRESHOLD = 0.30


class EnterPriceVsFifoCheck(CheckSpec):
    """Оприходование — «создание товара из ничего», цена напрямую бьёт по себестоимости.

    Сигналы: цена 0 при ненулевом FIFO (кейс бесплатных этикеток LLM отличит по комментарию)
    и заметное отклонение от цены ПРЕДЫДУЩЕГО поступления того же товара.

    Сравнивать с текущим FIFO нельзя: он средневзвешенный по остатку, а оприходование
    фиксирует цену конкретной покупки. Стрейч-плёнку покупали по 498, 720 и 733 ₽ при
    средней 576 ₽ — три «отклонения» на ровном месте."""

    id = 'enter_price_vs_fifo'
    section = Section.WAREHOUSE
    title = 'Цена оприходования расходится с себестоимостью'

    async def detect(self, ctx: AuditContext, since: datetime | None) -> list[RawFinding]:
        filters = (f'updated>={format_moment(since)}' if since
                   else f'moment>={ctx.scan_since_moment}')
        docs = await ctx.client.list_entities(
            ctx.session, 'enter',
            filters=filters,
            expand='positions.assortment',
            order='moment,desc',
        )
        fifo = await ctx.stock_fifo_map()
        uoms = await ctx.uom_map()
        sources, _ = await _product_source_docs(ctx)
        out = []
        for d in docs:
            suspicious = []
            for p in d.get('positions', {}).get('rows', []):
                a = p.get('assortment', {})
                href = a.get('meta', {}).get('href', '').split('?')[0]
                fifo_price, _stock = fifo.get(href, (0, 0))
                price = p.get('price', 0)
                previous = previous_receipt_price(sources.get(href, []), (d.get('moment') or '')[:19])
                prev_price = (previous or {}).get('price_kopecks') or 0
                if fifo_price and price == 0:
                    kind = 'цена 0 при ненулевой себестоимости'
                elif (prev_price
                      and abs(price - prev_price) / prev_price > _FIFO_DEVIATION_THRESHOLD):
                    kind = 'отклонение от цены прошлого поступления'
                else:
                    continue
                suspicious.append({
                    'product': a.get('name', '?'),
                    'quantity': p.get('quantity', 0),
                    'uom': uoms.get(href, ''),
                    'price_kopecks': price,
                    'previous_receipt': previous,
                    'current_fifo_kopecks': fifo_price,
                    'signal': kind,
                })
            if suspicious:
                out.append(RawFinding(
                    entity_type='enter',
                    entity_id=d['id'],
                    entity_href=d['meta']['href'],
                    entity_name=f'Оприходование №{d["name"]} от {d["moment"][:10]}',
                    severity=self.default_severity,
                    payload={
                        'description': (d.get('description') or '')[:300],
                        'moment': (d.get('moment') or '')[:16],
                        'positions': suspicious,
                    },
                    fingerprint_salt='|'.join(sorted(s['product'] for s in suspicious)),
                ))
        return out

    def explain(self, payload: dict) -> str:
        return ('Цены позиций оприходования расходятся с текущей себестоимостью — '
                'FIFO готовой продукции исказится.')


class NegativeStockCheck(CheckSpec):
    """Отрицательный остаток на конкретном складе — учёт разъехался с реальностью."""

    id = 'negative_stock'
    section = Section.WAREHOUSE
    title = 'Отрицательный остаток на складе'
    default_severity = Severity.CRITICAL
    llm_triage = False   # математически однозначно, объяснение не требует суждения

    async def detect(self, ctx: AuditContext, since: datetime | None) -> list[RawFinding]:
        rows = await ctx.client.stock_by_store_negative(ctx.session)
        out = []
        for r in rows:
            product_href = r.get('meta', {}).get('href', '').split('?')[0]
            for s in r.get('stockByStore', []):
                if s.get('stock', 0) >= 0:
                    continue
                store = s.get('name', '?')
                out.append(RawFinding(
                    entity_type='product',
                    entity_id=product_href.split('/')[-1],
                    entity_href=product_href,
                    entity_name=f'{r.get("name", "?")} — склад «{store}»',
                    severity=self.default_severity,
                    payload={'product': r.get('name'), 'store': store,
                             'stock': s.get('stock')},
                    fingerprint_salt=store,
                ))
        return out

    def explain(self, payload: dict) -> str:
        return (f'Остаток {payload.get("stock")} на складе «{payload.get("store")}». '
                f'Товар ушёл в минус: продано/списано/израсходовано больше, чем числится. '
                f'Проверь приёмки, перемещения и производственные задания по этому товару.')
