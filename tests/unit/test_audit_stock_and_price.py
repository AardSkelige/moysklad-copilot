import pytest

from services.audit.checks.negative_stock import NegativeStockCheck
from services.audit.checks.sale_price import DemandPriceVsRetailCheck
from services.audit.checks.warehouse import EnterPriceVsFifoCheck
from tests.unit.test_audit_checks import MS, FakeClient, FakeContext, _doc, _product_meta

pytestmark = [pytest.mark.asyncio, pytest.mark.unit, pytest.mark.audit]

CANISTER = f'{MS}/entity/product/p5'
UI_LINK = 'https://online.moysklad.ru/app/#good/edit?id=ui-p5'


class StockClient(FakeClient):
    def __init__(self, operations=None, docs=None, **kwargs):
        super().__init__(**kwargs)
        self.operations = operations or []
        self.docs = docs or {}

    async def stock_by_store_negative(self, session):
        return [{'meta': {'href': f'{CANISTER}?expand=supplier'},
                 'stockByStore': [{'name': 'Готовая продукция', 'stock': -1.0},
                                  {'name': 'Производство', 'stock': 0.0}]}]

    async def get_by_href(self, session, href):
        if href == CANISTER:
            return {'name': 'Кондиционер Черника-василёк 5000 мл',
                    'meta': {'href': CANISTER, 'uuidHref': UI_LINK}}
        return self.docs.get(href)

    async def product_operations(self, session, product_href, since):
        return self.operations


def _operation(entity, doc_id, name, moment, quantity, store='Готовая продукция'):
    return {'operation': {'meta': {'href': f'{MS}/entity/{entity}/{doc_id}', 'type': entity},
                          'name': name, 'moment': moment},
            'store': {'name': store}, 'quantity': quantity}


def _swap_500_for_5000():
    return [{'moment': '2026-11-09 11:43:30.000', 'eventType': 'update', 'diff': {'positions': [
        {'oldValue': {'assortment': {'name': 'Кондиционер Черника-василёк 500 мл'},
                      'quantity': 1.0, 'price': 5395.0}},
        {'newValue': {'assortment': {'name': 'Кондиционер Черника-василёк 5000 мл'},
                      'quantity': 1.0, 'price': 5395.0}},
    ]}}]


class TestNegativeStock:
    async def test_names_the_product_and_links_to_its_card(self):
        found = await NegativeStockCheck().detect(FakeContext(StockClient()), None)
        assert len(found) == 1
        assert found[0].entity_name == 'Кондиционер Черника-василёк 5000 мл — склад «Готовая продукция»'
        assert found[0].payload['product'] == 'Кондиционер Черника-василёк 5000 мл'
        assert found[0].ui_link == UI_LINK

    async def test_shows_the_late_edit_that_caused_the_minus(self):
        demand_href = f'{MS}/entity/demand/d931'
        client = StockClient(
            operations=[
                _operation('productionstagecompletion', 'c772', '00772', '2026-10-20 09:47:00.000', 1.0),
                _operation('demand', 'd931', '00931', '2026-10-20 09:47:00.000', -1.0),
                _operation('demand', 'd992', '00992', '2026-10-24 17:49:00.000', -1.0),
            ],
            docs={
                demand_href: {'id': 'd931', 'name': '00931', 'moment': '2026-10-20 09:47:00.000',
                              'created': '2026-10-20 09:48:00.000',
                              'updated': '2026-11-09 11:43:30.000'},
                f'{MS}/entity/demand/d992': {
                    'id': 'd992', 'name': '00992', 'moment': '2026-10-24 17:49:00.000',
                    'created': '2026-10-24 17:49:38.000', 'updated': '2026-10-24 17:53:05.000'},
                f'{MS}/entity/productionstagecompletion/c772': {
                    'id': 'c772', 'name': '00772', 'moment': '2026-10-20 09:47:00.000',
                    'created': '2026-10-20 09:47:10.000', 'updated': '2026-10-20 09:47:10.000'},
            },
            audit_events=_swap_500_for_5000(),
        )
        found = await NegativeStockCheck().detect(FakeContext(client), None)
        payload = found[0].payload
        assert payload['recent_operations'] == [
            '20.10 09:47 Выполнение этапа №00772: +1',
            '20.10 09:47 Отгрузка №00931: −1',
            '24.10 17:49 Отгрузка №00992: −1',
        ]
        assert len(payload['late_edits']) == 1
        edit = payload['late_edits'][0]
        assert edit['document'] == 'Отгрузка №00931 от 20.10'
        assert edit['edited'] == '09.11 11:43'
        assert any('500 мл' in line for line in edit['changes'])
        assert any('5000 мл' in line for line in edit['changes'])
        text = NegativeStockCheck().explain(payload)
        assert 'Отгрузка №00931 от 20.10' in text and '09.11 11:43' in text

    async def test_other_store_operations_are_left_out(self):
        client = StockClient(operations=[
            _operation('move', 'm1', '00010', '2026-10-20 09:47:00.000', 5.0, store='Производство'),
        ])
        found = await NegativeStockCheck().detect(FakeContext(client), None)
        assert found[0].payload['recent_operations'] == []

    async def test_broken_history_still_reports_the_minus(self):
        class Broken(StockClient):
            async def product_operations(self, session, product_href, since):
                raise TimeoutError('отчёт недоступен')

        found = await NegativeStockCheck().detect(FakeContext(Broken()), None)
        assert found[0].payload['stock'] == -1.0
        assert found[0].payload['history_checked'] is False


def _demand(doc_id, name, price, retail, product='Кондиционер Черника-василёк 500 мл',
            discount=0, kind='product', **extra):
    assortment = {'name': product, 'salePrices': [{'value': retail}],
                  'meta': {'href': f'{MS}/entity/{kind}/{doc_id}-p', 'type': kind}}
    return _doc('demand', doc_id, name, '2026-10-20 09:47:00',
                agent={'name': 'ООО "ЯНДЕКС.МАРКЕТ"'},
                positions={'rows': [{'price': price, 'quantity': 1, 'discount': discount,
                                     'assortment': assortment}]},
                **extra)


class TestDemandPriceVsRetail:
    async def _found(self, demand):
        ctx = FakeContext(FakeClient({'demand': [demand]}))
        return await DemandPriceVsRetailCheck().detect(ctx, None)

    async def test_small_bottle_sold_at_canister_price(self):
        found = await self._found(_demand('d1', '00931', 539500, 65000))
        assert len(found) == 1
        position = found[0].payload['positions'][0]
        assert position['signal'] == 'дороже розницы в 8,3 раза'
        assert 'Кондиционер Черника-василёк 500 мл' in DemandPriceVsRetailCheck().explain(found[0].payload)

    async def test_too_cheap_is_flagged(self):
        found = await self._found(_demand('d2', '00238', 1201, 65000))
        assert found[0].payload['positions'][0]['signal'] == 'дешевле розницы в 54,1 раза'

    async def test_marketplace_markup_is_normal(self):
        assert await self._found(_demand('d3', '00482', 180000, 65000)) == []

    async def test_dealer_price_is_normal(self):
        assert await self._found(_demand('d4', '00314', 29000, 65000)) == []

    async def test_discount_counts_toward_the_price(self):
        assert await self._found(_demand('d5', '00500', 250000, 65000, discount=50)) == []

    async def test_zero_price_and_missing_retail_are_skipped(self):
        assert await self._found(_demand('d6', '00501', 0, 65000)) == []
        assert await self._found(_demand('d7', '00502', 539500, 0)) == []

    async def test_services_are_skipped(self):
        assert await self._found(_demand('d8', '00503', 539500, 65000, kind='service')) == []


def _packaging_enter(description):
    stock = [{'meta': {'href': f'{MS}/entity/product/box'}, 'name': 'Картонный короб M',
              'price': 2841, 'stock': 30}]
    box = {**_product_meta('box', 'Картонный короб M'), 'pathName': 'Хоз. товары/Упаковка'}
    enter = _doc('enter', 'e11', '00011-00001', '2026-11-09 15:45:00', description=description,
                 positions={'rows': [{'price': 0, 'quantity': 22, 'assortment': box}]})
    return FakeContext(FakeClient({'enter': [enter]}, stock=stock))


class TestFreePackagingEnter:
    async def test_free_in_comment_silences_zero_price(self):
        ctx = _packaging_enter('Лена: коробки от ХорсБио, бесплатно')
        assert await EnterPriceVsFifoCheck().detect(ctx, None) == []

    async def test_free_source_marker_silences_zero_price(self, monkeypatch):
        monkeypatch.setattr('services.audit.checks.products.FREE_SOURCE_MARKERS', ('с хб',))
        ctx = _packaging_enter('Лена: принесено с хб')
        assert await EnterPriceVsFifoCheck().detect(ctx, None) == []

    async def test_unexplained_zero_says_packaging_is_outside_product_cost(self, monkeypatch):
        monkeypatch.setattr('services.audit.checks.products.FREE_SOURCE_MARKERS', ())
        monkeypatch.setattr('services.audit.checks.warehouse.NON_COST_FOLDERS', ('Хоз',))
        found = await EnterPriceVsFifoCheck().detect(_packaging_enter('Лена: пересчёт'), None)
        assert len(found) == 1
        assert found[0].payload['positions'][0]['in_product_cost'] is False
        assert 'не входят в техкарты' in found[0].payload['note']
