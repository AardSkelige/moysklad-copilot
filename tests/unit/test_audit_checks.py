"""Тесты детекторов аудита на канированных ответах API.

Фикстуры повторяют живые кейсы реального аккаунта МойСклад:
дубль платежей 00082/00086, нулевые этикетки в приёмке, основа по 52,20 при FIFO 60,27.
"""

import pytest

from services.audit.checks.cross import RetroEditCheck
from services.audit.checks.money import PaymentDuplicateCheck
from services.audit.checks.products import FifoDeviationCheck
from services.audit.checks.purchases import SupplyZeroPriceCheck
from services.audit.checks.sales import DemandZeroCheck
from services.audit.checks.warehouse import EnterPriceVsFifoCheck
from services.audit.specs import fingerprint

pytestmark = [pytest.mark.asyncio, pytest.mark.unit, pytest.mark.audit]

MS = 'https://api.moysklad.ru/api/remap/1.2'


def _doc(entity, doc_id, name, moment, updated=None, applicable=True, **extra):
    return {
        'id': doc_id,
        'name': name,
        'moment': moment,
        'updated': updated or moment,
        'applicable': applicable,
        'meta': {'href': f'{MS}/entity/{entity}/{doc_id}', 'type': entity},
        **extra,
    }


class FakeClient:
    """Канированные ответы вместо API. data: {entity: [rows]}."""

    def __init__(self, data=None, stock=None, audit_events=None, batches=None):
        self.data = data or {}
        self.stock = stock or []
        self.audit_events = audit_events or []
        self.batches = batches or {}

    async def list_entities(self, session, entity, **kwargs):
        return self.data.get(entity, [])

    async def stock_all(self, session, stock_mode='all'):
        return self.stock

    async def stock_by_store_negative(self, session):
        return []

    async def entity_audit_events(self, session, entity, entity_id):
        return self.audit_events

    async def stock_batches(self, session, product_id):
        return self.batches.get(product_id, [])


class FakeContext:
    def __init__(self, client):
        from datetime import datetime
        self.client = client
        self.session = None
        self.now = datetime.now()
        self.scan_since_moment = '2026-04-01 00:00:00'
        self._fifo = {
            r['meta']['href'].split('?')[0]: (r.get('price', 0), r.get('stock', 0))
            for r in client.stock
        }
        self._uom = {
            r['meta']['href'].split('?')[0]: ((r.get('uom') or {}).get('name') or '')
            for r in client.stock
        }

    async def stock_rows(self):
        return self.client.stock

    async def stock_fifo_map(self):
        return self._fifo

    async def uom_map(self):
        return self._uom

    async def cached_list(self, key, entity, **kwargs):
        return await self.client.list_entities(self.session, entity, **kwargs)


def _product_meta(pid, name):
    return {'name': name, 'meta': {'href': f'{MS}/entity/product/{pid}', 'type': 'product'}}


class TestSupplyZeroPrice:
    async def test_detects_zero_positions(self):
        supply = _doc('supply', 's1', '00055', '2026-07-01 10:00:00', positions={'rows': [
            {'price': 0, 'quantity': 300, 'assortment': _product_meta('p1', 'Этикетка | Пробник 50 мл')},
            {'price': 1500, 'quantity': 10, 'assortment': _product_meta('p2', 'Флакон 500 мл')},
        ]})
        ctx = FakeContext(FakeClient({'supply': [supply]}))
        found = await SupplyZeroPriceCheck().detect(ctx, None)
        assert len(found) == 1
        assert found[0].payload['zero_count'] == 1
        assert found[0].payload['zero_positions'][0]['product'] == 'Этикетка | Пробник 50 мл'

    async def test_ignores_priced_supply(self):
        supply = _doc('supply', 's2', '00060', '2026-07-01 10:00:00', positions={'rows': [
            {'price': 1500, 'quantity': 10, 'assortment': _product_meta('p2', 'Флакон')},
        ]})
        ctx = FakeContext(FakeClient({'supply': [supply]}))
        assert await SupplyZeroPriceCheck().detect(ctx, None) == []

    async def test_fingerprint_changes_with_new_zero_position(self):
        check = SupplyZeroPriceCheck()
        base = _doc('supply', 's1', '00055', '2026-07-01 10:00:00', positions={'rows': [
            {'price': 0, 'quantity': 1, 'assortment': _product_meta('p1', 'Этикетка А')},
        ]})
        ctx = FakeContext(FakeClient({'supply': [base]}))
        fp1 = fingerprint(check.id, (await check.detect(ctx, None))[0])
        fp1_again = fingerprint(check.id, (await check.detect(ctx, None))[0])
        assert fp1 == fp1_again  # стабильность

        base['positions']['rows'].append(
            {'price': 0, 'quantity': 2, 'assortment': _product_meta('p3', 'Этикетка Б')})
        fp2 = fingerprint(check.id, (await check.detect(ctx, None))[0])
        assert fp1 != fp2  # новая нулевая позиция = новый сигнал


class TestPaymentDuplicate:
    def _payment(self, doc_id, name, moment, total, agent_href='a1'):
        return _doc('paymentout', doc_id, name, moment,
                    sum=total,
                    agent={'name': 'ХИМТОРГ ПРИМЕР', 'meta': {'href': f'{MS}/entity/counterparty/{agent_href}'}},
                    paymentPurpose=f'Оплата по заказу 00049')

    async def test_detects_live_duplicate_00082_00086(self):
        payments = [
            self._payment('pm1', '00082', '2026-06-15 14:59:00', 2107000),
            self._payment('pm2', '00086', '2026-06-18 22:28:00', 2107000),
        ]
        ctx = FakeContext(FakeClient({'paymentout': payments}))
        found = await PaymentDuplicateCheck().detect(ctx, None)
        assert len(found) == 1
        assert len(found[0].payload['payments']) == 2
        assert found[0].payload['sum_kopecks'] == 2107000

    async def test_different_sums_not_duplicate(self):
        payments = [
            self._payment('pm1', '00082', '2026-06-15 14:59:00', 2107000),
            self._payment('pm2', '00086', '2026-06-18 22:28:00', 185000),
        ]
        ctx = FakeContext(FakeClient({'paymentout': payments}))
        assert await PaymentDuplicateCheck().detect(ctx, None) == []

    async def test_draft_payment_excluded(self):
        payments = [
            self._payment('pm1', '00082', '2026-06-15 14:59:00', 2107000),
            {**self._payment('pm2', '00086', '2026-06-18 22:28:00', 2107000),
             'applicable': False},
        ]
        ctx = FakeContext(FakeClient({'paymentout': payments}))
        assert await PaymentDuplicateCheck().detect(ctx, None) == []

    async def test_monthly_recurring_not_duplicate(self):
        # Ежемесячные комиссии банка / подписка МойСклад — не дубль
        payments = [
            self._payment('pm1', '00010', '2026-04-19 10:00:00', 169000),
            self._payment('pm2', '00050', '2026-05-19 10:00:00', 169000),
            self._payment('pm3', '00088', '2026-06-19 10:00:00', 169000),
        ]
        ctx = FakeContext(FakeClient({'paymentout': payments}))
        assert await PaymentDuplicateCheck().detect(ctx, None) == []


class TestEnterPriceVsFifo:
    async def test_detects_live_case_osnova(self):
        # Основа по 39,00 при прошлом поступлении 60,27 — отклонение 35%, выше порога 30%
        stock = [{'meta': {'href': f'{MS}/entity/product/p1'}, 'name': 'Основа шампуня 500 мл',
                  'price': 6027, 'stock': 155}]
        earlier = _doc('enter', 'e0', '00001', '2026-06-01 12:00:00',
                       positions={'rows': [
                           {'price': 6027, 'quantity': 10,
                            'assortment': _product_meta('p1', 'Основа шампуня 500 мл')},
                       ]})
        enter = _doc('enter', 'e1', '00002-00003', '2026-06-18 12:00:00',
                     description='Аня оприходовала основы',
                     positions={'rows': [
                         {'price': 3900, 'quantity': 7,
                          'assortment': _product_meta('p1', 'Основа шампуня 500 мл')},
                     ]})
        ctx = FakeContext(FakeClient({'enter': [earlier, enter]}, stock=stock))
        found = await EnterPriceVsFifoCheck().detect(ctx, None)
        assert len(found) == 1
        assert found[0].payload['positions'][0]['signal'] == 'отклонение от цены прошлого поступления'

    async def test_zero_price_with_nonzero_fifo(self):
        stock = [{'meta': {'href': f'{MS}/entity/product/p1'}, 'name': 'Диметилфталат',
                  'price': 44, 'stock': 23400}]
        enter = _doc('enter', 'e2', '00004-00005', '2026-06-24 12:00:00', positions={'rows': [
            {'price': 0, 'quantity': 100, 'assortment': _product_meta('p1', 'Диметилфталат')},
        ]})
        ctx = FakeContext(FakeClient({'enter': [enter]}, stock=stock))
        found = await EnterPriceVsFifoCheck().detect(ctx, None)
        assert found[0].payload['positions'][0]['signal'] == 'цена 0 при ненулевой себестоимости'

    async def test_zero_fifo_zero_price_is_ok(self):
        # Бесплатные этикетки: FIFO 0, цена 0 — сигнала нет
        stock = [{'meta': {'href': f'{MS}/entity/product/p1'}, 'name': 'Этикетка', 'price': 0, 'stock': 12}]
        enter = _doc('enter', 'e3', '00004-00004', '2026-06-24 12:00:00', positions={'rows': [
            {'price': 0, 'quantity': 9, 'assortment': _product_meta('p1', 'Этикетка')},
        ]})
        ctx = FakeContext(FakeClient({'enter': [enter]}, stock=stock))
        assert await EnterPriceVsFifoCheck().detect(ctx, None) == []


class TestFifoDeviation:
    """Живой кейс: Лауроилглутамат натрия, 95% — сырьё в ГРАММАХ.

    Приёмка №00065 — 0,45 ₽/г (45 копеек), оплачена ровно на сумму заказа;
    приёмка №00079 — 1,70 ₽/г. FIFO 0,8079 ₽/г, отклонение 52,5%.
    """

    STOCK = [{'meta': {'href': f'{MS}/entity/product/p1'},
              'name': 'Лауроилглутамат натрия, 95%',
              'price': 80.7866311051842, 'stock': 5999.0,
              'uom': {'name': 'г'}, 'folder': {'name': 'Сырьё'}}]

    def _supplies(self):
        return [
            _doc('supply', 's79', '00079', '2026-08-05 17:52:00',
                 description='Яна: приняла на склад производства.',
                 sum=650000, payedSum=650000,
                 positions={'rows': [
                     {'price': 170, 'quantity': 1000,
                      'assortment': _product_meta('p1', 'Лауроилглутамат натрия, 95%')}]}),
            _doc('supply', 's65', '00065', '2026-07-19 13:32:00',
                 description='Яна: приняла на склад производства.',
                 sum=1125000, payedSum=1125000,
                 positions={'rows': [
                     {'price': 45, 'quantity': 5000,
                      'assortment': _product_meta('p1', 'Лауроилглутамат натрия, 95%')}]}),
        ]

    async def _detect(self):
        ctx = FakeContext(FakeClient({'supply': self._supplies()}, stock=self.STOCK))
        return await FifoDeviationCheck().detect(ctx, None)

    async def test_unit_of_measure_from_stock_report(self):
        found = await self._detect()
        assert len(found) == 1
        assert found[0].payload['uom'] == 'г'   # не «кг», которое LLM подставляла сама

    async def test_stock_value_shows_scale_of_problem(self):
        found = await self._detect()
        # 5999 г × 0,8079 ₽ ≈ 4 846 ₽ — цена вопроса, а не «весь остаток сырья»
        assert found[0].payload['stock_value_kopecks'] == pytest.approx(484639, abs=2)

    async def test_paid_supply_marked_as_confirmed_by_money(self):
        found = await self._detect()
        payload = found[0].payload
        assert payload['last_supply']['payment'] == 'оплачен полностью (6 500,00 ₽)'
        paid_states = [s['payment'] for s in payload['source_documents']]
        assert 'оплачен полностью (11 250,00 ₽)' in paid_states

    async def test_explain_names_the_unit(self):
        found = await self._detect()
        text = FifoDeviationCheck().explain(found[0].payload)
        assert '0.81 ₽/г' in text and '1.70 ₽/г' in text


class TestOrderSupplyMismatch:
    def _order(self, state_name, **extra):
        return _doc('purchaseorder', 'po1', '00056', '2026-07-01 10:00:00',
                    state={'name': state_name}, **extra)

    async def test_awaiting_status_silences_underdelivery(self):
        # Живой кейс №00056: статус «Заказано», предоплата есть, приёмок нет — норма
        from services.audit.checks.purchases import OrderSupplyMismatchCheck
        order = self._order('Заказано', sum=465700, shippedSum=0, payedSum=465700)
        ctx = FakeContext(FakeClient({'purchaseorder': [order]}))
        assert await OrderSupplyMismatchCheck().detect(ctx, None) == []

    async def test_accepted_status_underdelivery_detected(self):
        from services.audit.checks.purchases import OrderSupplyMismatchCheck
        order = self._order('Принято', sum=465700, shippedSum=100000, payedSum=465700)
        ctx = FakeContext(FakeClient({'purchaseorder': [order]}))
        found = await OrderSupplyMismatchCheck().detect(ctx, None)
        assert len(found) == 1
        assert 'приёмки меньше заказа' in found[0].payload['signals']
        assert found[0].payload['status'] == 'Принято'

    async def test_overpayment_signals_even_when_awaiting(self):
        from services.audit.checks.purchases import OrderSupplyMismatchCheck
        order = self._order('Заказано', sum=465700, shippedSum=0, payedSum=500000)
        ctx = FakeContext(FakeClient({'purchaseorder': [order]}))
        found = await OrderSupplyMismatchCheck().detect(ctx, None)
        assert found[0].payload['signals'] == ['оплачено больше суммы заказа']


class TestDemandZero:
    async def test_internal_agent_silent(self):
        # Служебный контрагент для внутренних передач — нулевая сумма ожидаема
        demand = _doc('demand', 'd9', '00176', '2026-08-10 14:46:00', sum=0,
                      agent={'name': 'StarPony - внутренние операции',
                             'meta': {'href': f'{MS}/entity/counterparty/int'}})
        ctx = FakeContext(FakeClient({'demand': [demand]}))
        assert await DemandZeroCheck().detect(ctx, None) == []

    async def test_named_reason_silences(self):
        # Живые формулировки: подарок, призы, спонсорство, комиссия по договору
        for comment in ('Лена: трек-номер СДЭК — 102967, подарок.',
                        'Лена: на призы на ЧР-2026 по конкуру.',
                        'Лена: спонсорство, пакеты, открытки.',
                        'Лена: скидка 30%, комиссия такая по договору.'):
            demand = _doc('demand', 'dz', '00103', '2026-07-20 10:00:00', sum=0,
                          description=comment)
            ctx = FakeContext(FakeClient({'demand': [demand]}))
            assert await DemandZeroCheck().detect(ctx, None) == [], comment

    async def test_reason_in_order_comment_also_silences(self):
        # По стандарту причина часто живёт в заказе, а не в отгрузке
        demand = _doc('demand', 'dz2', '00114', '2026-07-27 10:00:00', sum=0,
                      description='Накладные расходы 0 — самовывоз.',
                      customerOrder={'name': '00115',
                                     'description': 'Лена: на призы на ЧР-2026 по конкуру.'})
        ctx = FakeContext(FakeClient({'demand': [demand]}))
        assert await DemandZeroCheck().detect(ctx, None) == []

    async def test_detects_zero_demand_with_comment_in_payload(self):
        # Комментарий есть, но причины нулевой суммы в нём нет — разбирает LLM
        demand = _doc('demand', 'd1', '00077', '2026-07-03 10:00:00', sum=0,
                      description='Лена: трек-номер СДЭК — 10289519381.')
        ctx = FakeContext(FakeClient({'demand': [demand]}))
        found = await DemandZeroCheck().detect(ctx, None)
        assert len(found) == 1
        assert found[0].payload['description'] == 'Лена: трек-номер СДЭК — 10289519381.'

    async def test_nonzero_demand_ignored(self):
        demand = _doc('demand', 'd2', '00078', '2026-07-03 10:00:00', sum=150000)
        ctx = FakeContext(FakeClient({'demand': [demand]}))
        assert await DemandZeroCheck().detect(ctx, None) == []

    async def test_order_comment_in_payload(self):
        # По стандарту причина нулевой суммы живёт в комментарии связанного заказа
        demand = _doc('demand', 'd3', '00019', '2026-05-01 10:00:00', sum=0,
                      customerOrder={'id': 'o1', 'name': '00017',
                                     'description': 'Ира: самовывоз, забрали 01.05.'})
        ctx = FakeContext(FakeClient({'demand': [demand]}))
        found = await DemandZeroCheck().detect(ctx, None)
        assert found[0].payload['customer_order'] == 'Заказ покупателя №00017'
        assert found[0].payload['order_comment'] == 'Ира: самовывоз, забрали 01.05.'


class TestDemandNoOverhead:
    async def test_no_overhead_detected_with_comment_in_payload(self):
        from services.audit.checks.sales import DemandNoOverheadCheck
        demand = _doc('demand', 'd1', '00022', '2026-05-12 10:00:00', sum=0,
                      description='Ира: трек-номер СДЭК — 10266157756.')
        ctx = FakeContext(FakeClient({'demand': [demand]}))
        found = await DemandNoOverheadCheck().detect(ctx, None)
        assert len(found) == 1
        assert 'СДЭК' in found[0].payload['description']

    async def test_order_comment_in_payload(self):
        demand = _doc('demand', 'd3', '00030', '2026-05-21 10:00:00', sum=0,
                      customerOrder={'id': 'o1', 'name': '00032',
                                     'description': 'Оля: забирает для себя.'})
        from services.audit.checks.sales import DemandNoOverheadCheck
        ctx = FakeContext(FakeClient({'demand': [demand]}))
        found = await DemandNoOverheadCheck().detect(ctx, None)
        assert found[0].payload['order_comment'] == 'Оля: забирает для себя.'

    async def test_self_delivery_silent(self):
        # Самовывоз и «Своими силами» — услуги перевозчика нет, накладных не бывает
        from services.audit.checks.sales import DemandNoOverheadCheck
        for method in ('Самовывоз', 'Своими силами'):
            demand = _doc('demand', f'd_{method}', '00122', '2026-07-31 10:00:00', sum=1319500,
                          attributes=[{'name': 'Способ доставки', 'value': {'name': method}}])
            ctx = FakeContext(FakeClient({'demand': [demand]}))
            assert await DemandNoOverheadCheck().detect(ctx, None) == [], method

    async def test_delivery_method_in_payload(self):
        # Способ доставки берём из карточки, а не заставляем ИИ выводить из комментария
        from services.audit.checks.sales import DemandNoOverheadCheck
        demand = _doc('demand', 'd8', '00203', '2026-08-14 12:18:00', sum=4743000,
                      attributes=[{'name': 'Способ доставки', 'value': {'name': 'Яндекс (ПВЗ)'}}])
        ctx = FakeContext(FakeClient({'demand': [demand]}))
        found = await DemandNoOverheadCheck().detect(ctx, None)
        assert found[0].payload['delivery_method'] == 'Яндекс (ПВЗ)'

    async def test_marketplace_demand_silent(self):
        # Озон забирает товар в ПВЗ и везёт сам — накладных расходов не бывает
        from services.audit.checks.sales import DemandNoOverheadCheck
        demand = _doc('demand', 'd5', '00204', '2026-08-16 10:00:00', sum=1000000,
                      agent={'name': 'ООО "Ozon Маркетплейс"',
                             'meta': {'href': f'{MS}/entity/counterparty/oz'}})
        ctx = FakeContext(FakeClient({'demand': [demand]}))
        assert await DemandNoOverheadCheck().detect(ctx, None) == []

    async def test_with_overhead_silent(self):
        from services.audit.checks.sales import DemandNoOverheadCheck
        demand = _doc('demand', 'd2', '00023', '2026-05-12 10:00:00', sum=150000,
                      overhead={'sum': 51941})
        ctx = FakeContext(FakeClient({'demand': [demand]}))
        assert await DemandNoOverheadCheck().detect(ctx, None) == []

    async def test_fresh_demand_gets_grace(self):
        # Накладные расходы часто вносят на следующий день — сутки не сигналим
        from datetime import datetime
        from services.audit.checks.sales import DemandNoOverheadCheck
        moment = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        demand = _doc('demand', 'd4', '00085', moment, sum=150000)
        ctx = FakeContext(FakeClient({'demand': [demand]}))
        assert await DemandNoOverheadCheck().detect(ctx, None) == []


class TestDemandOverheadPayment:
    async def test_overhead_without_payment_detected(self):
        from services.audit.checks.sales import DemandOverheadPaymentCheck
        demand = _doc('demand', 'd1', '00022', '2026-05-12 10:00:00', sum=150000,
                      overhead={'sum': 51941})
        ctx = FakeContext(FakeClient({'demand': [demand], 'paymentout': [], 'cashout': []}))
        found = await DemandOverheadPaymentCheck().detect(ctx, None)
        assert len(found) == 1
        assert found[0].payload['overhead_kopecks'] == 51941

    async def test_matching_payment_silences(self):
        from services.audit.checks.sales import DemandOverheadPaymentCheck
        demand = _doc('demand', 'd1', '00022', '2026-05-12 10:00:00', sum=150000,
                      overhead={'sum': 51941})
        payment = _doc('paymentout', 'p1', '00050', '2026-05-14 10:00:00', sum=51941)
        ctx = FakeContext(FakeClient({'demand': [demand], 'paymentout': [payment],
                                      'cashout': []}))
        assert await DemandOverheadPaymentCheck().detect(ctx, None) == []

    async def test_payment_too_far_in_time_not_matched(self):
        from services.audit.checks.sales import DemandOverheadPaymentCheck
        demand = _doc('demand', 'd1', '00022', '2026-05-12 10:00:00', sum=150000,
                      overhead={'sum': 51941})
        payment = _doc('paymentout', 'p1', '00090', '2026-07-01 10:00:00', sum=51941)
        ctx = FakeContext(FakeClient({'demand': [demand], 'paymentout': [payment],
                                      'cashout': []}))
        assert len(await DemandOverheadPaymentCheck().detect(ctx, None)) == 1

    async def test_fresh_demand_gets_grace(self):
        # Живой кейс №00085: платёж за доставку проводят на следующий день
        from datetime import datetime
        from services.audit.checks.sales import DemandOverheadPaymentCheck
        moment = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        demand = _doc('demand', 'd5', '00085', moment, sum=150000,
                      overhead={'sum': 54991})
        ctx = FakeContext(FakeClient({'demand': [demand], 'paymentout': [],
                                      'cashout': []}))
        assert await DemandOverheadPaymentCheck().detect(ctx, None) == []

    async def test_sdek_delivery_excluded(self):
        # СДЭК выставляет сводный счёт — парного платежа на отгрузку не бывает
        from services.audit.checks.sales import DemandOverheadPaymentCheck
        demand = _doc('demand', 'd6', '00087', '2026-05-12 10:00:00', sum=150000,
                      overhead={'sum': 51941},
                      attributes=[{'name': 'Способ доставки',
                                   'value': {'name': 'СДЭК (ПВЗ)'}}])
        ctx = FakeContext(FakeClient({'demand': [demand], 'paymentout': [],
                                      'cashout': []}))
        assert await DemandOverheadPaymentCheck().detect(ctx, None) == []

    async def test_other_delivery_method_flagged_with_payload(self):
        # Живой кейс №00148 (ТК Байкал): накладные 8 434,29 ₽ без парного платежа.
        # Перевозчик выставляет счёт под конкретную перевозку — платёж обязан быть
        from services.audit.checks.sales import DemandOverheadPaymentCheck
        demand = _doc('demand', 'd7', '00148', '2026-08-07 15:00:00', sum=8847000,
                      overhead={'sum': 843429},
                      attributes=[{'name': 'Способ доставки',
                                   'value': {'name': 'ТК Байкал'}}])
        ctx = FakeContext(FakeClient({'demand': [demand], 'paymentout': [],
                                      'cashout': []}))
        found = await DemandOverheadPaymentCheck().detect(ctx, None)
        assert len(found) == 1
        assert found[0].payload['delivery_method'] == 'ТК Байкал'
        assert 'БЕЗ привязки' in found[0].payload['fix_hint']
        assert 'create_payment' in found[0].payload['fix_hint']


class TestEnterPriceSpread:
    async def test_price_spread_between_purchases_is_not_an_error(self):
        # Стрейч-плёнку покупали по 4,98 / 7,20 / 7,33 ₽ при средней 5,76 ₽:
        # сравнение с ПРОШЛОЙ покупкой, а не со средней, разброс не считает ошибкой
        from services.audit.checks.warehouse import EnterPriceVsFifoCheck
        stock = [{'meta': {'href': f'{MS}/entity/product/p7'}, 'name': 'Стрейч плёнка',
                  'price': 57627, 'stock': 4}]
        first = _doc('enter', 'x1', '00012', '2026-06-10 10:00:00', positions={'rows': [
            {'price': 72015, 'quantity': 1, 'assortment': _product_meta('p7', 'Стрейч плёнка')}]})
        second = _doc('enter', 'x2', '00034', '2026-08-12 10:00:00', positions={'rows': [
            {'price': 73280, 'quantity': 1, 'assortment': _product_meta('p7', 'Стрейч плёнка')}]})
        ctx = FakeContext(FakeClient({'enter': [first, second]}, stock=stock))
        found = await EnterPriceVsFifoCheck().detect(ctx, None)
        assert [f.entity_name for f in found] == []


class TestRootProduct:
    def _product(self, pid, name, code='', folder_id=None):
        d = {'id': pid, 'name': name, 'code': code,
             'meta': {'href': f'{MS}/entity/product/{pid}', 'type': 'product'}}
        if folder_id:
            d['productFolder'] = {'meta': {'href': f'{MS}/entity/productfolder/{folder_id}'}}
        return d

    async def test_product_in_root_detected(self):
        # Живой кейс: «Пробники тары» с кодом 0 в корне справочника
        from services.audit.checks.products import RootProductCheck
        data = {'product': [self._product('p1', 'Пробники тары', '0'),
                            self._product('p2', 'Флакон 50 мл', '4-022', folder_id='f1')],
                'productfolder': [{'id': 'f1', 'name': 'Тара',
                                   'meta': {'href': f'{MS}/entity/productfolder/f1'}}]}
        found = await RootProductCheck().detect(FakeContext(FakeClient(data)), None)
        assert len(found) == 1
        assert found[0].entity_name == 'Пробники тары'
        # аналитику подсказываем существующие папки с их схемой кодов
        assert found[0].payload['folders_available'] == ['Тара (коды 4-xxx)']

    async def test_products_in_folders_silent(self):
        from services.audit.checks.products import RootProductCheck
        data = {'product': [self._product('p2', 'Флакон 50 мл', '4-022', folder_id='f1')],
                'productfolder': [{'id': 'f1', 'name': 'Тара',
                                   'meta': {'href': f'{MS}/entity/productfolder/f1'}}]}
        assert await RootProductCheck().detect(FakeContext(FakeClient(data)), None) == []

    async def test_archived_root_product_ignored(self):
        from services.audit.checks.products import RootProductCheck
        prod = self._product('p3', 'Старый товар', '0')
        prod['archived'] = True
        data = {'product': [prod], 'productfolder': []}
        assert await RootProductCheck().detect(FakeContext(FakeClient(data)), None) == []


class TestDeliveryAsPosition:
    def _doc_with_delivery(self, doc_id, name, overhead=0):
        return _doc('supply', doc_id, name, '2026-06-21 10:00:00', sum=1354000,
                    overhead={'sum': overhead, 'distribution': 'price'},
                    positions={'rows': [
                        {'price': 139, 'quantity': 6000,
                         'assortment': _product_meta('p1', 'БТМС (BTMS) 80%')},
                        {'price': 100, 'quantity': 1000,
                         'assortment': _product_meta('p2', 'Доставка (для закупок)')}]})

    async def test_double_counted_delivery_exposed(self):
        # Живой кейс 00051: доставка и позицией, и накладными расходами
        from services.audit.checks.cross import DeliveryAsPositionCheck
        ctx = FakeContext(FakeClient({'supply': [self._doc_with_delivery('s1', '00051', 100000)]}))
        found = await DeliveryAsPositionCheck().detect(ctx, None)
        assert len(found) == 1
        assert found[0].payload['overhead_kopecks'] == 100000

    async def test_delivery_without_overhead_marked(self):
        # 00049: накладных нет — стоимость доставки не попала в себестоимость
        from services.audit.checks.cross import DeliveryAsPositionCheck
        ctx = FakeContext(FakeClient({'supply': [self._doc_with_delivery('s2', '00049')]}))
        found = await DeliveryAsPositionCheck().detect(ctx, None)
        assert found[0].payload['overhead_kopecks'] == 0


class TestRetroEdit:
    async def test_detects_retro_edited_supply(self):
        # Живой кейс: приёмка 00025 от 01.04 исправлена 11.06 (+70 дней)
        supply = _doc('supply', 's1', '00025', '2026-04-01 10:00:00',
                      updated='2026-06-11 15:00:00', description='исправление приёмки')
        ctx = FakeContext(FakeClient({'supply': [supply]}))
        found = await RetroEditCheck().detect(ctx, None)
        assert len(found) == 1
        assert found[0].payload['gap_days'] > 69

    async def test_same_day_edit_ok(self):
        supply = _doc('supply', 's2', '00049', '2026-06-21 10:00:00',
                      updated='2026-06-21 18:00:00')
        ctx = FakeContext(FakeClient({'supply': [supply]}))
        assert await RetroEditCheck().detect(ctx, None) == []

    async def test_draft_not_flagged(self):
        supply = _doc('supply', 's3', '00099', '2026-04-01 10:00:00',
                      updated='2026-07-01 10:00:00', applicable=False)
        ctx = FakeContext(FakeClient({'supply': [supply]}))
        assert await RetroEditCheck().detect(ctx, None) == []

    async def test_backdated_creation_is_not_an_edit(self):
        # Живой кейс: приёмка 00081 от 04.08 заведена в МС 07.08 и больше не менялась —
        # разрыв даёт само оформление постфактум, правки после создания не было
        supply = _doc('supply', 's4', '00081', '2026-08-04 10:24:00',
                      updated='2026-08-07 10:24:26', created='2026-08-07 10:24:26',
                      description='Яна приняла на склад производства')
        ctx = FakeContext(FakeClient({'supply': [supply]}))
        assert await RetroEditCheck().detect(ctx, None) == []

    async def test_edit_after_backdated_creation_detected(self):
        # тот же документ, но потом его действительно правили — сигнал обязан остаться
        supply = _doc('supply', 's5', '00081', '2026-08-04 10:24:00',
                      updated='2026-08-15 12:00:00', created='2026-08-07 10:24:26')
        ctx = FakeContext(FakeClient({'supply': [supply]}))
        found = await RetroEditCheck().detect(ctx, None)
        assert len(found) == 1
        assert found[0].payload['gap_days'] > 8      # считаем от создания, не от даты документа
        assert found[0].payload['created'] == '2026-08-07 10:24'

    async def test_comment_only_edit_is_not_flagged(self):
        # Живой кейс: в оприходовании 00032 через сутки переписали только комментарий
        supply = _doc('enter', 'e9', '00032', '2026-08-05 14:25:00',
                      updated='2026-08-07 09:48:00', created='2026-08-05 14:25:00')
        events = [{'moment': '2026-08-07 09:48:57', 'eventType': 'update', 'uid': 'admin@x',
                   'diff': {'description': {'oldValue': 'заказ 1337055',
                                            'newValue': 'Лена: заказ 1337055'}}}]
        ctx = FakeContext(FakeClient({'enter': [supply]}, audit_events=events))
        assert await RetroEditCheck().detect(ctx, None) == []

    async def test_position_rewritten_with_same_values_is_cosmetic(self):
        # МойСклад пишет позицию в diff и когда её перезаписали теми же значениями:
        # состав не менялся, FIFO не двигался — будить владельца незачем
        same = {'assortment': {'name': 'БТМС (BTMS) 80%'},
                'quantity': 6000.0, 'uom': 'г', 'price': 1.39}
        supply = _doc('supply', 'e11', '00051', '2026-06-21 13:00:00',
                      updated='2026-07-05 22:26:00', created='2026-06-21 13:07:00')
        events = [{'moment': '2026-07-05 22:26:00', 'eventType': 'update', 'uid': 'admin@x',
                   'diff': {'description': {'oldValue': 'Яна приняла', 'newValue': 'Яна: приняла.'},
                            'positions': [{'oldValue': same, 'newValue': dict(same)}]}}]
        ctx = FakeContext(FakeClient({'supply': [supply]}, audit_events=events))
        assert await RetroEditCheck().detect(ctx, None) == []

    async def test_position_change_is_readable_and_flagged(self):
        supply = _doc('enter', 'e10', '00033', '2026-08-05 14:25:00',
                      updated='2026-08-07 09:48:00', created='2026-08-05 14:25:00')
        events = [{'moment': '2026-08-07 09:48:57', 'eventType': 'update', 'uid': 'admin@x',
                   'diff': {'sum': {'oldValue': 1192.9, 'newValue': 2885.4},
                            'positions': [{'newValue': {
                                'assortment': {'name': 'Короб чёрный'},
                                'quantity': 25.0, 'uom': 'шт', 'price': 3.6}}]}}]
        ctx = FakeContext(FakeClient({'enter': [supply]}, audit_events=events))
        found = await RetroEditCheck().detect(ctx, None)
        assert len(found) == 1
        change = found[0].payload['changes_after_doc_date'][0]
        assert change['who'] == 'admin@x'
        assert change['diff']['positions'] == ['добавлена позиция: Короб чёрный — 25 шт по 3,60 ₽']

    async def test_edits_within_first_day_are_data_entry(self):
        # Живой кейс 00085: комментарий дописан через 14 секунд после создания —
        # это ввод, в историю поздних правок он попадать не должен
        supply = _doc('supply', 's6', '00085', '2026-08-12 12:32:00',
                      updated='2026-08-14 14:50:00', created='2026-08-12 12:33:00')
        events = [
            {'moment': '2026-08-14 14:50:37', 'eventType': 'update', 'uid': 'admin@x',
             'diff': {'positions': [{'newValue': {'assortment': {'name': 'Этикетка'},
                                                  'quantity': 42.0, 'uom': 'шт', 'price': 23.8}}]}},
            {'moment': '2026-08-12 12:33:53', 'eventType': 'update', 'uid': 'admin@x',
             'diff': {'description': {'oldValue': '', 'newValue': 'Лена забирает сама'}}},
        ]
        ctx = FakeContext(FakeClient({'supply': [supply]}, audit_events=events))
        found = await RetroEditCheck().detect(ctx, None)
        assert len(found) == 1
        changes = found[0].payload['changes_after_doc_date']
        assert len(changes) == 1 and 'positions' in changes[0]['diff']

    async def test_linked_documents_in_payload(self):
        supply = _doc('supply', 's7', '00085', '2026-08-12 12:32:00',
                      updated='2026-08-14 14:50:00', created='2026-08-12 12:33:00',
                      purchaseOrder={'name': '00083', 'moment': '2026-08-12 12:30:00',
                                     'sum': 200000.0,
                                     'meta': {'type': 'purchaseorder'}})
        ctx = FakeContext(FakeClient({'supply': [supply]}))
        found = await RetroEditCheck().detect(ctx, None)
        assert found[0].payload['linked_documents'] == [
            'Заказ поставщику №00083 от 2026-08-12 на 2 000,00 ₽']

    async def test_comment_edit_does_not_renew_fingerprint(self):
        # Живой баг: ревью комментариев причесало комментарий приёмки 00085, у документа
        # обновился updated — и уже разобранная находка пришла владельцу заново
        check = RetroEditCheck()
        supply = _doc('supply', 's8', '00085', '2026-08-12 12:32:00',
                      updated='2026-08-14 14:50:00', created='2026-08-12 12:33:00')
        events = [{'moment': '2026-08-14 14:50:37', 'eventType': 'update', 'uid': 'admin@x',
                   'diff': {'positions': [{'newValue': {'assortment': {'name': 'Этикетка'},
                                                        'quantity': 42.0, 'uom': 'шт',
                                                        'price': 23.8}}]}}]
        ctx = FakeContext(FakeClient({'supply': [supply]}, audit_events=events))
        fp1 = fingerprint(check.id, (await check.detect(ctx, None))[0])

        # бот поправил комментарий 18.08 — документ «изменился», но учёт не затронут
        supply['updated'] = '2026-08-18 09:04:00'
        events.insert(0, {'moment': '2026-08-18 09:04:00', 'eventType': 'update', 'uid': 'admin@x',
                          'diff': {'description': {'oldValue': 'Яна приняла',
                                                   'newValue': 'Яна: приняла.'}}})
        fp2 = fingerprint(check.id, (await check.detect(ctx, None))[0])
        assert fp1 == fp2   # тот же сигнал, повторного уведомления быть не должно

    async def test_fingerprint_new_on_another_day_edit(self):
        check = RetroEditCheck()
        supply = _doc('supply', 's1', '00025', '2026-04-01 10:00:00',
                      updated='2026-06-11 15:00:00')
        ctx = FakeContext(FakeClient({'supply': [supply]}))
        fp1 = fingerprint(check.id, (await check.detect(ctx, None))[0])
        supply['updated'] = '2026-06-20 15:00:00'
        fp2 = fingerprint(check.id, (await check.detect(ctx, None))[0])
        assert fp1 != fp2


class TestOverheadPaymentMatching:
    def _demand(self, name, moment, overhead):
        return _doc('demand', f'd{name}', name, moment, sum=8847000,
                    overhead={'sum': overhead, 'distribution': 'price'},
                    agent={'name': 'КСЦ ПРИМЕР',
                           'meta': {'href': f'{MS}/entity/counterparty/c1'}})

    def _payment(self, name, moment, total, purpose=''):
        return _doc('paymentout', f'p{name}', name, moment, sum=total,
                    paymentPurpose=purpose)

    async def test_matched_by_document_number_in_purpose(self):
        # Стандарт аккаунта: связь платежа с документом — текстом в назначении.
        # Сумма счёта перевозчика отличается от накладных расходов — это не повод сигналить
        from services.audit.checks.sales import DemandOverheadPaymentCheck
        data = {
            'demand': [self._demand('00148', '2026-08-07 15:00:00', 843429)],
            'paymentout': [self._payment('00120', '2026-08-09 10:00:00', 800000,
                                         'Доставка по отгрузке № 00148')],
        }
        ctx = FakeContext(FakeClient(data))
        assert await DemandOverheadPaymentCheck().detect(ctx, None) == []

    async def test_same_number_but_other_document_type_not_matched(self):
        # «Приёмка № 00148» — тот же номер, но платёж не про нашу отгрузку
        from services.audit.checks.sales import DemandOverheadPaymentCheck
        data = {
            'demand': [self._demand('00148', '2026-08-07 15:00:00', 843429)],
            'paymentout': [self._payment('00120', '2026-08-09 10:00:00', 800000,
                                         'Доставка по приёмке № 00148')],
        }
        ctx = FakeContext(FakeClient(data))
        found = await DemandOverheadPaymentCheck().detect(ctx, None)
        assert len(found) == 1

    async def test_yandex_pvz_is_consolidated_carrier(self):
        # Яндекс/Озон ПВЗ, как и СДЭК, выставляют сводный счёт за период
        from services.audit.checks.sales import DemandOverheadPaymentCheck
        demand = self._demand('00082', '2026-07-07 15:00:00', 48922)
        demand['attributes'] = [{'name': 'Способ доставки', 'value': {'name': 'Яндекс (ПВЗ)'}}]
        ctx = FakeContext(FakeClient({'demand': [demand]}))
        assert await DemandOverheadPaymentCheck().detect(ctx, None) == []

    async def test_still_matched_by_equal_sum(self):
        from services.audit.checks.sales import DemandOverheadPaymentCheck
        data = {
            'demand': [self._demand('00150', '2026-08-07 15:00:00', 843429)],
            'paymentout': [self._payment('00121', '2026-08-08 10:00:00', 843429)],
        }
        ctx = FakeContext(FakeClient(data))
        assert await DemandOverheadPaymentCheck().detect(ctx, None) == []


class TestDuplicateCounterparty:
    def _agent(self, aid, name, inn='', created='2026-02-24 10:00:00', archived=False):
        return {'id': aid, 'name': name, 'inn': inn, 'created': created,
                'archived': archived,
                'meta': {'href': f'{MS}/entity/counterparty/{aid}', 'type': 'counterparty'}}

    async def test_same_inn_detected(self):
        # Живой кейс: Озон заведён дважды с одним ИНН, документы разошлись
        from services.audit.checks.money import DuplicateCounterpartyCheck
        data = {'counterparty': [
            self._agent('a1', 'ООО "ИНТЕРНЕТ РЕШЕНИЯ"', '7704217370'),
            self._agent('a2', 'ООО "Интернет решения"', '7704217370', created='2026-08-17 10:00:00'),
        ]}
        found = await DuplicateCounterpartyCheck().detect(FakeContext(FakeClient(data)), None)
        assert len(found) == 1
        assert found[0].payload['match_by'] == 'ИНН'

    async def test_archived_duplicate_ignored(self):
        # объединённый дубль уже в архиве — сигнала быть не должно
        from services.audit.checks.money import DuplicateCounterpartyCheck
        data = {'counterparty': [
            self._agent('a1', 'ООО "ЛАДОГА ПЛЮС"', '7805274783'),
            self._agent('a2', 'ООО "ЛАДОГА ПЛЮС"', '7805274783', archived=True),
        ]}
        assert await DuplicateCounterpartyCheck().detect(FakeContext(FakeClient(data)), None) == []

    async def test_same_name_without_inn_detected(self):
        from services.audit.checks.money import DuplicateCounterpartyCheck
        data = {'counterparty': [self._agent('a1', 'ТК Байкал'), self._agent('a2', 'ТК Байкал')]}
        found = await DuplicateCounterpartyCheck().detect(FakeContext(FakeClient(data)), None)
        assert found[0].payload['match_by'] == 'название'

    async def test_different_agents_silent(self):
        from services.audit.checks.money import DuplicateCounterpartyCheck
        data = {'counterparty': [self._agent('a1', 'Лемун', '111'),
                                 self._agent('a2', 'Полицвет', '222')]}
        assert await DuplicateCounterpartyCheck().detect(FakeContext(FakeClient(data)), None) == []


class TestCounterpartyBalance:
    def _agent(self, href='a1', name='ХИМТОРГ ПРИМЕР'):
        return {'name': name, 'meta': {'href': f'{MS}/entity/counterparty/{href}'}}

    async def test_overpaid_supplier_detected(self):
        from services.audit.checks.money import CounterpartyBalanceCheck
        # приёмок на 21 070, оплачено дважды — переплата 21 070 (живой кейс)
        data = {
            'supply': [_doc('supply', 's1', '00049', '2026-06-21 10:00:00', sum=2107000,
                            agent=self._agent())],
            'paymentout': [
                _doc('paymentout', 'p1', '00082', '2026-06-15 10:00:00', sum=2107000,
                     agent=self._agent()),
                _doc('paymentout', 'p2', '00086', '2026-06-18 10:00:00', sum=2107000,
                     agent=self._agent()),
            ],
        }
        ctx = FakeContext(FakeClient(data))
        found = await CounterpartyBalanceCheck().detect(ctx, None)
        assert len(found) == 1
        assert 'переплата поставщику' in found[0].payload['signals'][0]
        # суммы в payload — в рублях: аналитик путался в конвертации копеек
        assert found[0].payload['paid_out_rub'] == 42140.0

    async def test_commission_agent_debt_counts_sold_only(self):
        # Живой кейс Каприоля: отгружено на реализацию 327 763 ₽, продано по отчёту
        # комиссионера 78 680 ₽ — долгом является только проданное
        from services.audit.checks.money import CounterpartyBalanceCheck
        agent = self._agent('cap', 'КСЦ Каприоль')
        data = {
            'demand': [_doc('demand', 'd1', '00100', '2026-07-01 10:00:00',
                            sum=32776300, agent=agent)],
            'contract': [{'id': 'c1', 'contractType': 'Commission',
                          'agent': {'meta': {'href': f'{MS}/entity/counterparty/cap'}},
                          'meta': {'href': f'{MS}/entity/contract/c1'}}],
            'commissionreportin': [_doc('commissionreportin', 'r1', '00008',
                                        '2026-07-28 10:00:00', sum=7868000, agent=agent)],
        }
        found = await CounterpartyBalanceCheck().detect(FakeContext(FakeClient(data)), None)
        assert len(found) == 1
        p = found[0].payload
        assert p['sold_by_commission_rub'] == 78680.0
        assert p['goods_on_partner_shelf_rub'] == 249083.0
        assert '78 680.00' in p['signals'][0]

    async def test_commission_agent_settled_is_silent(self):
        # продано по отчёту и оплачено — расхождения нет, хотя отгружено больше
        from services.audit.checks.money import CounterpartyBalanceCheck
        agent = self._agent('cap2', 'ИП Комиссионер')
        data = {
            'demand': [_doc('demand', 'd1', '00101', '2026-07-01 10:00:00',
                            sum=1975000, agent=agent)],
            'contract': [{'id': 'c2', 'contractType': 'Commission',
                          'agent': {'meta': {'href': f'{MS}/entity/counterparty/cap2'}},
                          'meta': {'href': f'{MS}/entity/contract/c2'}}],
            'commissionreportin': [_doc('commissionreportin', 'r2', '00009',
                                        '2026-07-28 10:00:00', sum=854000, agent=agent)],
            'paymentin': [_doc('paymentin', 'p1', '00050', '2026-07-29 10:00:00',
                               sum=854000, agent=agent)],
        }
        assert await CounterpartyBalanceCheck().detect(FakeContext(FakeClient(data)), None) == []

    async def test_marketplace_debt_is_not_a_problem(self):
        # Озон платит реестром за период — долг по отгрузкам выравнивается выплатой
        from services.audit.checks.money import CounterpartyBalanceCheck
        ozon = self._agent('oz', 'ООО "Ozon Маркетплейс"')
        data = {'demand': [_doc('demand', 'd1', '00204', '2026-08-16 10:00:00',
                                sum=11728000, agent=ozon)]}
        ctx = FakeContext(FakeClient(data))
        assert await CounterpartyBalanceCheck().detect(ctx, None) == []

    async def test_marketplace_overpayment_still_flagged(self):
        # получено больше, чем отгружено — аномалия даже для маркетплейса
        from services.audit.checks.money import CounterpartyBalanceCheck
        ozon = self._agent('oz', 'ООО "Ozon Маркетплейс"')
        data = {
            'demand': [_doc('demand', 'd1', '00204', '2026-08-16 10:00:00',
                            sum=100000, agent=ozon)],
            'paymentin': [_doc('paymentin', 'pi1', '00300', '2026-08-17 10:00:00',
                               sum=500000, agent=ozon)],
        }
        ctx = FakeContext(FakeClient(data))
        found = await CounterpartyBalanceCheck().detect(ctx, None)
        assert len(found) == 1
        assert 'получено больше' in found[0].payload['signals'][0]

    async def test_balanced_counterparty_silent(self):
        from services.audit.checks.money import CounterpartyBalanceCheck
        data = {
            'supply': [_doc('supply', 's1', '00050', '2026-06-21 10:00:00', sum=100000,
                            agent=self._agent())],
            'paymentout': [_doc('paymentout', 'p1', '00090', '2026-06-22 10:00:00',
                                sum=100000, agent=self._agent())],
        }
        ctx = FakeContext(FakeClient(data))
        assert await CounterpartyBalanceCheck().detect(ctx, None) == []

    async def test_no_closing_docs_payment_excluded_from_balance(self):
        # Стандарт: платёж за доставку = галка «Без закрывающих документов».
        # МойСклад исключает его из взаиморасчётов — мы зеркалим (живой кейс)
        from services.audit.checks.money import CounterpartyBalanceCheck
        data = {
            'supply': [_doc('supply', 's1', '00058', '2026-07-04 10:00:00', sum=1380000,
                            agent=self._agent(), overhead={'sum': 185000})],
            'paymentout': [
                _doc('paymentout', 'p1', '00093', '2026-07-04 10:00:00', sum=1380000,
                     agent=self._agent()),
                _doc('paymentout', 'p2', '00094', '2026-07-04 10:00:00', sum=185000,
                     agent=self._agent(), noClosingDocs=True),
            ],
        }
        ctx = FakeContext(FakeClient(data))
        assert await CounterpartyBalanceCheck().detect(ctx, None) == []

    async def test_prepaid_open_order_is_not_overpayment(self):
        # Кейс Тара.ру: заказ «Заказано» оплачен, тара едет — аванс, не переплата
        from services.audit.checks.money import CounterpartyBalanceCheck
        data = {
            'supply': [_doc('supply', 's1', '00040', '2026-05-20 10:00:00', sum=774990,
                            agent=self._agent('tara', 'ТАРА.РУ'))],
            'paymentout': [_doc('paymentout', 'p1', '00080', '2026-05-20 10:00:00',
                                sum=774990, agent=self._agent('tara', 'ТАРА.РУ'))],
            'cashout': [_doc('cashout', 'c1', '00097', '2026-07-05 10:00:00',
                             sum=465700, agent=self._agent('tara', 'ТАРА.РУ'))],
            'purchaseorder': [_doc('purchaseorder', 'o1', '00056', '2026-07-01 10:00:00',
                                   sum=465700, shippedSum=0,
                                   agent=self._agent('tara', 'ТАРА.РУ'))],
        }
        ctx = FakeContext(FakeClient(data))
        assert await CounterpartyBalanceCheck().detect(ctx, None) == []

    async def test_overpayment_without_open_order_still_flagged(self):
        from services.audit.checks.money import CounterpartyBalanceCheck
        data = {
            'supply': [_doc('supply', 's1', '00040', '2026-05-20 10:00:00', sum=774990,
                            agent=self._agent('tara', 'ТАРА.РУ'))],
            'paymentout': [_doc('paymentout', 'p1', '00080', '2026-05-20 10:00:00',
                                sum=1240690, agent=self._agent('tara', 'ТАРА.РУ'))],
            'purchaseorder': [],
        }
        ctx = FakeContext(FakeClient(data))
        found = await CounterpartyBalanceCheck().detect(ctx, None)
        assert len(found) == 1
        assert 'переплата поставщику' in found[0].payload['signals'][0]

    async def test_unpaid_customer_order_facts_in_payload(self):
        # Живой кейс Александровой: отгрузка не оплачена, но заказ «Ждем оплату»
        # с договорённостью в комментарии — факты уходят LLM, судит он
        from services.audit.checks.money import CounterpartyBalanceCheck
        agent = self._agent('olga', 'Александрова Ольга')
        data = {
            'demand': [_doc('demand', 'd1', '00081', '2026-07-07 10:00:00',
                            sum=60450, agent=agent)],
            'paymentin': [],
            'customerorder': [_doc('customerorder', 'o1', '00081', '2026-07-07 10:00:00',
                                   sum=60450, payedSum=0, agent=agent,
                                   state={'name': 'Ждем оплату'},
                                   description='Ира: самовывоз. Оплата будет после 10го.')],
        }
        ctx = FakeContext(FakeClient(data))
        found = await CounterpartyBalanceCheck().detect(ctx, None)
        assert len(found) == 1
        orders = found[0].payload['open_customer_orders']
        assert orders[0]['status'] == 'Ждем оплату'
        assert orders[0]['awaiting_payment_rub'] == 604.5
        assert 'после 10го' in orders[0]['comment']

    async def test_pure_payment_counterparty_skipped(self):
        # Банк/подписки: только платежи, товарных документов нет — не взаиморасчёт
        from services.audit.checks.money import CounterpartyBalanceCheck
        data = {
            'paymentout': [_doc('paymentout', 'p1', '00088', '2026-06-19 10:00:00',
                                sum=169000, agent=self._agent('bank', 'ООО БАНК ТОЧКА'))],
        }
        ctx = FakeContext(FakeClient(data))
        assert await CounterpartyBalanceCheck().detect(ctx, None) == []

    async def test_unpaid_demand_detected(self):
        from services.audit.checks.money import CounterpartyBalanceCheck
        data = {
            'demand': [_doc('demand', 'd1', '00070', '2026-06-25 10:00:00', sum=500000,
                            agent=self._agent('b1', 'ООО Маркетплейс'))],
        }
        ctx = FakeContext(FakeClient(data))
        found = await CounterpartyBalanceCheck().detect(ctx, None)
        assert len(found) == 1
        assert 'покупатель не доплатил' in found[0].payload['signals'][0]


class TestPaymentNoClosingDocs:
    def _payment(self, name, moment, **extra):
        return _doc('paymentout', f'p_{name}', name, moment, sum=185000,
                    paymentPurpose='Доставка по приёмке № 00058', **extra)

    async def test_old_unlinked_without_flag_detected(self):
        from services.audit.checks.money import PaymentNoClosingDocsCheck
        ctx = FakeContext(FakeClient({'paymentout': [
            self._payment('00094', '2026-06-01 10:00:00')]}))
        found = await PaymentNoClosingDocsCheck().detect(ctx, None)
        assert len(found) == 1
        assert found[0].payload['age_days'] >= 7

    async def test_with_flag_silent(self):
        from services.audit.checks.money import PaymentNoClosingDocsCheck
        ctx = FakeContext(FakeClient({'paymentout': [
            self._payment('00094', '2026-06-01 10:00:00', noClosingDocs=True)]}))
        assert await PaymentNoClosingDocsCheck().detect(ctx, None) == []

    async def test_linked_silent(self):
        from services.audit.checks.money import PaymentNoClosingDocsCheck
        ctx = FakeContext(FakeClient({'paymentout': [
            self._payment('00093', '2026-06-01 10:00:00',
                          operations=[{'meta': {'type': 'purchaseorder'}}])]}))
        assert await PaymentNoClosingDocsCheck().detect(ctx, None) == []

    async def test_fresh_prepayment_grace_period(self):
        # свежий аванс ещё ждёт приёмку — не сигналим
        from datetime import datetime, timedelta
        from services.audit.checks.money import PaymentNoClosingDocsCheck
        fresh = (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S')
        ctx = FakeContext(FakeClient({'paymentout': [self._payment('00099', fresh)]}))
        assert await PaymentNoClosingDocsCheck().detect(ctx, None) == []


class TestDeliveryAsPosition:
    async def test_delivery_position_in_purchase_detected(self):
        from services.audit.checks.cross import DeliveryAsPositionCheck
        order = _doc('purchaseorder', 'o1', '00040', '2026-06-25 10:00:00',
                     positions={'rows': [
                         {'price': 150000, 'quantity': 10,
                          'assortment': _product_meta('p1', 'Флакон 500 мл')},
                         {'price': 100000, 'quantity': 1,
                          'assortment': _product_meta('p2', 'Доставка')},
                     ]})
        ctx = FakeContext(FakeClient({'purchaseorder': [order]}))
        found = await DeliveryAsPositionCheck().detect(ctx, None)
        assert len(found) == 1
        assert found[0].payload['delivery_positions'][0]['position'] == 'Доставка'

    async def test_sales_documents_not_checked(self):
        # Решение владельца: в заказах покупателей и отгрузках доставку не проверяем
        from services.audit.checks.cross import DeliveryAsPositionCheck
        order = _doc('customerorder', 'o1', '00077', '2026-06-25 10:00:00',
                     positions={'rows': [
                         {'price': 100000, 'quantity': 1,
                          'assortment': _product_meta('p2', 'Доставка')},
                     ]})
        ctx = FakeContext(FakeClient({'customerorder': [order]}))
        assert await DeliveryAsPositionCheck().detect(ctx, None) == []

    async def test_no_delivery_silent(self):
        from services.audit.checks.cross import DeliveryAsPositionCheck
        order = _doc('purchaseorder', 'o2', '00041', '2026-06-25 10:00:00',
                     positions={'rows': [
                         {'price': 150000, 'quantity': 10,
                          'assortment': _product_meta('p1', 'Флакон 500 мл')},
                     ]})
        ctx = FakeContext(FakeClient({'purchaseorder': [order]}))
        assert await DeliveryAsPositionCheck().detect(ctx, None) == []


class TestFifoChecks:
    def _stock_row(self, pid, name, price, stock):
        return {'meta': {'href': f'{MS}/entity/product/{pid}'},
                'name': name, 'price': price, 'stock': stock,
                'folder': {'name': 'Этикетки'}}

    def _supply_with(self, pid, name, price):
        return _doc('supply', 's1', '00050', '2026-06-20 10:00:00', positions={'rows': [
            {'price': price, 'quantity': 10, 'assortment': _product_meta(pid, name)},
        ]})

    async def test_fifo_zero_with_stock_detected(self):
        from services.audit.checks.products import FifoZeroCheck
        client = FakeClient(
            {'supply': []},
            stock=[self._stock_row('p1', 'Этикетка | Репеллент 500', 0, 120)],
        )
        found = await FifoZeroCheck().detect(FakeContext(client), None)
        assert len(found) == 1
        assert found[0].payload['stock'] == 120

    async def test_free_marker_in_comment_silences(self):
        # Живой кейс: «Этикетки получены бесплатно — себестоимость 0 корректна».
        # Читать такой комментарий должен код, а не LLM на каждом прогоне
        from services.audit.checks.products import FifoZeroCheck
        supply = _doc('supply', 's1', '00071', '2026-07-29 10:00:00',
                      description='Яна: приняла на склад. Этикетки получены бесплатно.',
                      positions={'rows': [
                          {'price': 0, 'quantity': 63,
                           'assortment': _product_meta('p1', 'Этикетка | Пенка 200 мл')}]})
        client = FakeClient({'supply': [supply]},
                            stock=[self._stock_row('p1', 'Этикетка | Пенка 200 мл', 0, 58)])
        assert await FifoZeroCheck().detect(FakeContext(client), None) == []

    async def test_previously_priced_in_payload(self):
        # Те же этикетки когда-то покупали по 36,16 ₽ — аналитик должен это видеть
        from services.audit.checks.products import FifoZeroCheck
        paid = _doc('supply', 's3', '00024', '2026-04-01 10:00:00',
                    description='Яна: приняла на склад производства.',
                    positions={'rows': [
                        {'price': 3616, 'quantity': 700,
                         'assortment': _product_meta('p3', 'Этикетка | Шампунь 500 мл')}]})
        free = _doc('supply', 's4', '00060', '2026-07-07 10:00:00',
                    description='Яна: приняла на склад производства.',
                    positions={'rows': [
                        {'price': 0, 'quantity': 100,
                         'assortment': _product_meta('p3', 'Этикетка | Шампунь 500 мл')}]})
        client = FakeClient({'supply': [paid, free]},
                            stock=[self._stock_row('p3', 'Этикетка | Шампунь 500 мл', 0, 696)])
        found = await FifoZeroCheck().detect(FakeContext(client), None)
        assert len(found) == 1
        assert found[0].payload['previously_priced']['price_kopecks'] == 3616

    async def test_zero_without_explanation_still_detected(self):
        # Тот же ноль, но причины в комментарии нет — сигнал обязан остаться
        from services.audit.checks.products import FifoZeroCheck
        supply = _doc('supply', 's2', '00072', '2026-07-29 10:00:00',
                      description='Яна: приняла на склад производства.',
                      positions={'rows': [
                          {'price': 0, 'quantity': 63,
                           'assortment': _product_meta('p2', 'Ланолин')}]})
        client = FakeClient({'supply': [supply]},
                            stock=[self._stock_row('p2', 'Ланолин', 0, 9920)])
        found = await FifoZeroCheck().detect(FakeContext(client), None)
        assert len(found) == 1

    async def test_fifo_zero_without_stock_silent(self):
        from services.audit.checks.products import FifoZeroCheck
        client = FakeClient({'supply': []},
                            stock=[self._stock_row('p1', 'Этикетка', 0, 0)])
        assert await FifoZeroCheck().detect(FakeContext(client), None) == []

    async def test_multiple_batches_explain_deviation(self):
        # Живой кейс лауроилглутамата: 4999 г по 0,53 ₽ и 1000 г по 2,20 ₽ дают
        # средневзвешенные 0,81 ₽ — расхождение с последней приёмкой мнимое
        from services.audit.checks.products import FifoDeviationCheck
        supply = _doc('supply', 's9', '00079', '2026-08-05 10:00:00', sum=650000,
                      positions={'rows': [
                          {'price': 170, 'quantity': 1000,
                           'assortment': _product_meta('p9', 'Лауроилглутамат натрия, 95%')}]})
        client = FakeClient(
            {'supply': [supply]},
            stock=[self._stock_row('p9', 'Лауроилглутамат натрия, 95%', 81, 5999)],
            batches={'p9': [{'stock': 4999, 'costPerUnit': 53},
                            {'stock': 1000, 'costPerUnit': 219.69}]},
        )
        assert await FifoDeviationCheck().detect(FakeContext(client), None) == []

    async def test_overhead_share_is_not_a_deviation(self):
        # Живой кейс вазелина: FIFO выше цены позиции ровно на долю доставки
        # (накладные 375 ₽ при сумме позиций 720 ₽) — расхождения нет
        from services.audit.checks.products import FifoDeviationCheck
        supply = _doc('supply', 's1', '00077', '2026-08-04 10:00:00',
                      sum=72000, overhead={'sum': 37500, 'distribution': 'price'},
                      positions={'rows': [
                          {'price': 72, 'quantity': 1000,
                           'assortment': _product_meta('p1', 'Вазелин медицинский')}]})
        client = FakeClient({'supply': [supply]},
                            stock=[self._stock_row('p1', 'Вазелин медицинский', 109, 865)])
        assert await FifoDeviationCheck().detect(FakeContext(client), None) == []

    async def test_deviation_beyond_overhead_still_flagged(self):
        # Та же приёмка, но FIFO вдвое выше цены с накладными — это уже сигнал
        from services.audit.checks.products import FifoDeviationCheck
        supply = _doc('supply', 's2', '00077', '2026-08-04 10:00:00',
                      sum=72000, overhead={'sum': 37500, 'distribution': 'price'},
                      positions={'rows': [
                          {'price': 72, 'quantity': 1000,
                           'assortment': _product_meta('p2', 'Вазелин медицинский')}]})
        client = FakeClient({'supply': [supply]},
                            stock=[self._stock_row('p2', 'Вазелин медицинский', 220, 865)])
        found = await FifoDeviationCheck().detect(FakeContext(client), None)
        assert len(found) == 1
        assert round(found[0].payload['compared_with_kopecks']) == 110

    async def test_deviation_over_50_percent_critical(self):
        from services.audit.checks.products import FifoDeviationCheck
        # FIFO 156, последняя приёмка 100 000 — перепутаны единицы (живой паттерн БТМС)
        client = FakeClient(
            {'supply': [self._supply_with('p1', 'БТМС (BTMS) 80%', 100000)]},
            stock=[self._stock_row('p1', 'БТМС (BTMS) 80%', 156, 7000)],
        )
        found = await FifoDeviationCheck().detect(FakeContext(client), None)
        assert len(found) == 1
        assert found[0].severity.value == 'critical'
        assert found[0].payload['deviation_percent'] > 50

    async def test_small_deviation_silent(self):
        from services.audit.checks.products import FifoDeviationCheck
        client = FakeClient(
            {'supply': [self._supply_with('p1', 'Хлорид натрия', 1000)]},
            stock=[self._stock_row('p1', 'Хлорид натрия', 1050, 500)],
        )
        assert await FifoDeviationCheck().detect(FakeContext(client), None) == []

    async def test_new_supply_creates_new_fingerprint(self):
        from services.audit.checks.products import FifoDeviationCheck
        check = FifoDeviationCheck()
        client = FakeClient(
            {'supply': [self._supply_with('p1', 'Основа', 10000)]},
            stock=[self._stock_row('p1', 'Основа', 20000, 50)],
        )
        ctx = FakeContext(client)
        fp1 = fingerprint(check.id, (await check.detect(ctx, None))[0])
        client.data['supply'][0]['name'] = '00061'   # пришла новая приёмка
        ctx2 = FakeContext(client)
        fp2 = fingerprint(check.id, (await check.detect(ctx2, None))[0])
        assert fp1 != fp2


class TestProductionRetroEdit:
    def _task(self, doc_id, name, moment, updated, production_end=None, applicable=True):
        d = _doc('productiontask', doc_id, name, moment, updated=updated,
                 applicable=applicable, state={'name': 'готово'})
        if production_end:
            d['productionEnd'] = production_end
        return d

    async def test_edit_after_completion_detected(self):
        from services.audit.checks.production import ProductionRetroEditCheck
        task = self._task('t1', '00087', '2026-06-22 10:00:00',
                          updated='2026-07-04 16:00:00',
                          production_end='2026-06-24 18:00:00')
        ctx = FakeContext(FakeClient({'productiontask': [task]}))
        found = await ProductionRetroEditCheck().detect(ctx, None)
        assert len(found) == 1
        assert found[0].payload['gap_days_after_completion'] > 9

    async def test_edits_before_completion_are_workflow(self):
        # ПЗ создано давно, правится, но производство не завершено — норма
        from services.audit.checks.production import ProductionRetroEditCheck
        task = self._task('t2', '00091', '2026-06-01 10:00:00',
                          updated='2026-07-01 16:00:00', production_end=None)
        ctx = FakeContext(FakeClient({'productiontask': [task]}))
        assert await ProductionRetroEditCheck().detect(ctx, None) == []

    async def test_task_created_after_completion_is_not_an_edit(self):
        # ПЗ занесли в МС уже после того, как производство закончили — правок не было
        from services.audit.checks.production import ProductionRetroEditCheck
        task = self._task('t4', '00093', '2026-07-01 10:00:00',
                          updated='2026-07-10 12:00:00',
                          production_end='2026-07-02 18:00:00')
        task['created'] = '2026-07-10 12:00:00'
        ctx = FakeContext(FakeClient({'productiontask': [task]}))
        assert await ProductionRetroEditCheck().detect(ctx, None) == []

    async def test_same_day_completion_edit_ok(self):
        from services.audit.checks.production import ProductionRetroEditCheck
        task = self._task('t3', '00089', '2026-07-04 10:00:00',
                          updated='2026-07-04 17:00:00',
                          production_end='2026-07-04 16:57:00')
        ctx = FakeContext(FakeClient({'productiontask': [task]}))
        assert await ProductionRetroEditCheck().detect(ctx, None) == []
