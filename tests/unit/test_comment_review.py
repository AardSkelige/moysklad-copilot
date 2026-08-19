"""Тесты ревью комментариев: разбор ответа LLM, только suggest попадает в очередь."""

import json

import pytest

from services.audit.comment_review import (apply_demand, needs_final_dot,
                                           review_documents, review_finance_documents)

pytestmark = [pytest.mark.asyncio, pytest.mark.unit, pytest.mark.audit]


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def chat(self, history, tools=None):
        self.calls += 1
        return self.responses.pop(0)


def _doc(entity='supply', doc_id='d1', label='Приёмка №00049', comment='аня частичная приемка'):
    return {'entity': entity, 'id': doc_id, 'label': label,
            'agent': 'ХИМТОРГ ПРИМЕР', 'sum_rub': 17570.0, 'comment': comment}


class TestReviewDocuments:
    async def test_suggest_collected_ok_skipped(self):
        llm = FakeLLM([{'content': json.dumps([
            {'n': 0, 'verdict': 'suggest',
             'new_comment': 'Аня: частичная приёмка — прислали меньше SLES.',
             'reason': 'орфография и структура'},
            {'n': 1, 'verdict': 'ok'},
        ], ensure_ascii=False), 'tool_calls': []}])
        docs = [_doc(), _doc(doc_id='d2', comment='Аня: всё в порядке.')]
        out = await review_documents(docs, llm=llm)
        assert len(out) == 1
        assert out[0]['id'] == 'd1'
        assert out[0]['new_comment'].startswith('Аня:')

    async def test_json_wrapped_in_text_parsed(self):
        llm = FakeLLM([{'content': 'Вот результат:\n[{"n": 0, "verdict": "suggest", '
                                   '"new_comment": "Катя: списание брака.", "reason": "нет автора"}]',
                        'tool_calls': []}])
        out = await review_documents([_doc()], llm=llm)
        assert len(out) == 1

    async def test_broken_batch_does_not_crash(self):
        llm = FakeLLM([{'content': 'не json', 'tool_calls': []}])
        assert await review_documents([_doc()], llm=llm) == []

    async def test_batching_by_eight(self):
        resp = {'content': '[]', 'tool_calls': []}
        llm = FakeLLM([resp, resp, resp])
        docs = [_doc(doc_id=f'd{i}') for i in range(17)]
        await review_documents(docs, llm=llm)
        assert llm.calls == 3   # 8 + 8 + 1

    async def test_empty_new_comment_skipped(self):
        llm = FakeLLM([{'content': '[{"n": 0, "verdict": "suggest", "new_comment": ""}]',
                        'tool_calls': []}])
        assert await review_documents([_doc()], llm=llm) == []

    async def test_identical_suggestion_skipped(self):
        # LLM «предложил» тот же текст (кейс со скрина) — карточку не показываем
        llm = FakeLLM([{'content': json.dumps([{
            'n': 0, 'verdict': 'suggest',
            'new_comment': 'аня частичная приемка',
            'reason': 'добавлено двоеточие'}], ensure_ascii=False), 'tool_calls': []}])
        assert await review_documents([_doc()], llm=llm) == []

    async def test_whitespace_only_difference_skipped(self):
        llm = FakeLLM([{'content': json.dumps([{
            'n': 0, 'verdict': 'suggest',
            'new_comment': 'Аня  частичная\nприемка',
            'reason': 'переносы'}], ensure_ascii=False), 'tool_calls': []}])
        assert await review_documents([_doc()], llm=llm) == []


    async def test_empty_comment_not_invented(self):
        # пустое поле — норма: пересказ полей документа комментарием не является
        llm = FakeLLM([{'content': json.dumps([{
            'n': 0, 'verdict': 'suggest',
            'new_comment': 'Аня: приёмка от ХИМТОРГ ПРИМЕР на 17570.00 руб.',
            'reason': 'пустой комментарий'}], ensure_ascii=False), 'tool_calls': []}])
        assert await review_documents([_doc(comment='')], llm=llm) == []
    async def test_author_name_never_replaced(self):
        # заказ маркетплейса подписан Леной; правило зон ответственности ждёт Женю,
        # но подпись — факт: правится всё остальное, имя остаётся
        llm = FakeLLM([{'content': json.dumps([{
            'n': 0, 'verdict': 'suggest',
            'new_comment': 'Катя: отгрузка №87517655, заказ № 60505189377.',
            'reason': 'автор по контрагенту'}], ensure_ascii=False), 'tool_calls': []}])
        docs = [_doc(entity='customerorder', doc_id='o9', label='Заказ покупателя №00232',
                     comment='Ира: Отгрузка №87517655, Заказ № 60505189377')]
        out = await review_documents(docs, llm=llm)
        assert len(out) == 1
        assert out[0]['new_comment'] == 'Ира: отгрузка №87517655, заказ № 60505189377.'
    async def test_author_added_when_missing(self):
        # там, где имени нет вовсе, подставить автора по-прежнему можно
        llm = FakeLLM([{'content': json.dumps([{
            'n': 0, 'verdict': 'suggest',
            'new_comment': 'Аня: оплатила отдушку наличными.',
            'reason': 'нет автора'}], ensure_ascii=False), 'tool_calls': []}])
        out = await review_documents([_doc(comment='оплатила отдушку наличными')], llm=llm)
        assert out[0]['new_comment'].startswith('Аня:')


def _demand(doc_id='dm1', comment='', order_comment='', overhead=0.0, delivery=None):
    return {'entity': 'demand', 'id': doc_id, 'kind': 'demand',
            'label': 'Отгрузка №00030 от 2026-05-21', 'agent': 'Иванова Мария',
            'sum_rub': 0.0, 'comment': comment, 'overhead_rub': overhead,
            'delivery_method': delivery,
            'order_id': 'o1', 'order_name': '00032', 'order_comment': order_comment}

    async def test_only_dot_goes_to_batch_not_card(self):
        # правка «только точка» карточкой не показывается — она уходит в пакетную операцию
        llm = FakeLLM([{'content': json.dumps([{
            'n': 0, 'verdict': 'suggest',
            'new_comment': 'Ира: заказ 52050099-0627-1.',
            'reason': 'точка в конце'}], ensure_ascii=False), 'tool_calls': []}])
        docs = [_doc(entity='customerorder', comment='Ира: заказ 52050099-0627-1')]
        assert await review_documents(docs, llm=llm) == []

    async def test_dot_plus_real_fix_still_shown(self):
        # если вместе с точкой правится что-то ещё, карточка остаётся
        llm = FakeLLM([{'content': json.dumps([{
            'n': 0, 'verdict': 'suggest',
            'new_comment': 'Ира: заказ 52050099-0627-1.',
            'reason': 'регистр и точка'}], ensure_ascii=False), 'tool_calls': []}])
        docs = [_doc(entity='customerorder', comment='Ира: Заказ 52050099-0627-1')]
        assert len(await review_documents(docs, llm=llm)) == 1


class TestReviewFinance:
    @staticmethod
    def _fin(purpose='Заказ поставщику № 00074', comment=''):
        return {'kind': 'finance', 'entity': 'cashout', 'id': 'f1',
                'label': 'РКО №00058', 'agent': 'ИП Пример', 'sum_rub': 3582.0,
                'purpose': purpose, 'comment': comment, 'linked': ['Заказ поставщику № 00074']}

    async def test_empty_comment_not_invented(self):
        # комментарий из пустоты не сочиняем — он дублировал бы назначение платежа
        llm = FakeLLM([{'content': json.dumps([{
            'n': 0, 'new_purpose': None,
            'new_comment': 'Аня: оплата по заказу поставщику № 00074.',
            'reason': 'добавлен автор'}], ensure_ascii=False), 'tool_calls': []}])
        assert await review_finance_documents([self._fin()], llm=llm) == []

    async def test_empty_purpose_still_filled(self):
        # назначение, в отличие от комментария, восстанавливается по привязкам платежа
        llm = FakeLLM([{'content': json.dumps([{
            'n': 0, 'new_purpose': 'Заказ поставщику № 00074',
            'new_comment': None, 'reason': 'основание по привязке'},
        ], ensure_ascii=False), 'tool_calls': []}])
        out = await review_finance_documents([self._fin(purpose='')], llm=llm)
        assert len(out) == 1
        assert out[0]['new_purpose'] == 'Заказ поставщику № 00074'


class TestReviewDemands:
    """Отгрузки: комментарий только про накладные, остальное переносится в заказ."""

    async def test_clear_demand_and_move_to_order(self):
        # Тавтология в отгрузке удаляется, факт уезжает в заказ
        llm = FakeLLM([{'content': json.dumps([{
            'n': 0, 'new_comment': '',
            'new_order_comment': 'Оля: забирает для себя. Самовывоз.',
            'reason': 'пересказ полей удалён, причина перенесена в заказ'}],
            ensure_ascii=False), 'tool_calls': []}])
        docs = [_demand(comment='Ира: отгрузка внутренняя, сумма 0 руб.',
                        order_comment='Оля: забирает для себя.')]
        out = await review_documents(docs, llm=llm)
        assert len(out) == 1
        assert out[0]['new_comment'] == ''          # очистить отгрузку
        assert out[0]['new_order_comment'].startswith('Оля:')

    async def test_order_only_change(self):
        llm = FakeLLM([{'content': json.dumps([{
            'n': 0, 'new_comment': None,
            'new_order_comment': 'Ира: самовывоз. Трек-номер СДЭК — 123.',
            'reason': 'трек перенесён в заказ'}], ensure_ascii=False), 'tool_calls': []}])
        out = await review_documents([_demand(order_comment='Ира: самовывоз.')], llm=llm)
        assert len(out) == 1
        assert out[0]['new_comment'] is None
        assert 'Трек-номер' in out[0]['new_order_comment']

    async def test_no_changes_skipped(self):
        llm = FakeLLM([{'content': '[{"n": 0, "new_comment": null, '
                                   '"new_order_comment": null}]', 'tool_calls': []}])
        assert await review_documents([_demand(comment='Самовывоз.')], llm=llm) == []

    async def test_identical_suggestions_skipped(self):
        llm = FakeLLM([{'content': json.dumps([{
            'n': 0, 'new_comment': 'самовывоз.',
            'new_order_comment': 'Оля: забирает для себя.',
            'reason': 'ничего'}], ensure_ascii=False), 'tool_calls': []}])
        docs = [_demand(comment='Самовывоз.', order_comment='Оля: забирает для себя.')]
        assert await review_documents(docs, llm=llm) == []

    async def test_demands_use_separate_prompt_and_keep_doc_order(self):
        from services.audit import comment_review
        seen_systems = []

        class SpyLLM(FakeLLM):
            async def chat(self, history, tools=None):
                seen_systems.append(history[0]['content'])
                return await super().chat(history, tools)

        llm = SpyLLM([
            # сначала общий батч (не-отгрузки), потом батч отгрузок
            {'content': '[{"n": 0, "verdict": "suggest", '
                        '"new_comment": "Аня: частичная приёмка.", "reason": "стиль"}]',
             'tool_calls': []},
            {'content': '[{"n": 0, "new_comment": "Самовывоз — расходов нет.", '
                        '"new_order_comment": null, "reason": "пояснение нуля"}]',
             'tool_calls': []},
        ])
        docs = [_doc(), _demand()]
        out = await review_documents(docs, llm=llm)
        assert llm.calls == 2
        assert seen_systems[0] == comment_review._SYSTEM
        assert seen_systems[1] == comment_review._DEMAND_SYSTEM
        # порядок карточек = порядок исходного списка документов
        assert [s['id'] for s in out] == ['d1', 'dm1']

    async def test_paired_order_excluded_from_generic_review(self):
        # Заказ со связанной отгрузкой проверяется карточкой-парой,
        # иначе две карточки правили бы один комментарий вразнобой
        llm = FakeLLM([{'content': '[]', 'tool_calls': []}])
        order = _doc(entity='customerorder', doc_id='o1',
                     label='Заказ покупателя №00032', comment='оля забирает')
        await review_documents([order, _demand()], llm=llm)
        # один вызов LLM — только батч отгрузок; заказ в общий поток не попал
        assert llm.calls == 1

    async def test_overhead_reason_not_cleared(self):
        # кейс со скрина: комментарий уже поясняет накладные, LLM предложил стереть
        llm = FakeLLM([{'content': json.dumps([{
            'n': 0, 'new_comment': '',
            'new_order_comment': None,
            'reason': 'всё нужное есть в заказе'}], ensure_ascii=False), 'tool_calls': []}])
        docs = [_demand(comment='Накладные расходы 0 — самовывоз.')]
        assert await review_documents(docs, llm=llm) == []

    async def test_sdek_empty_comment_not_filled(self):
        # СДЭК: единый сводный счёт — пустую отгрузку пояснением накладных не дополняем
        llm = FakeLLM([{'content': json.dumps([{
            'n': 0, 'new_comment': 'Накладные расходы 0 — доставка СДЭК.',
            'new_order_comment': None,
            'reason': 'пояснение нуля'}], ensure_ascii=False), 'tool_calls': []}])
        docs = [_demand(delivery='СДЭК (до пункта выдачи)')]
        assert await review_documents(docs, llm=llm) == []

    async def test_sdek_existing_comment_still_editable(self):
        # правка стиля существующего текста для СДЭК остаётся разрешённой
        llm = FakeLLM([{'content': json.dumps([{
            'n': 0, 'new_comment': 'За доставку платила Ира со своего счёта.',
            'new_order_comment': None,
            'reason': 'стиль'}], ensure_ascii=False), 'tool_calls': []}])
        docs = [_demand(comment='за доставку платила ира', delivery='СДЭК', overhead=350.0)]
        out = await review_documents(docs, llm=llm)
        assert len(out) == 1

    async def test_delivery_method_passed_to_llm(self):
        seen_payloads = []

        class SpyLLM(FakeLLM):
            async def chat(self, history, tools=None):
                seen_payloads.append(history[1]['content'])
                return await super().chat(history, tools)

        llm = SpyLLM([{'content': '[]', 'tool_calls': []}])
        await review_documents([_demand(delivery='СДЭК')], llm=llm)
        payload = json.loads(seen_payloads[0])
        assert payload[0]['способ_доставки'] == 'СДЭК'

    async def test_second_demand_of_same_order_does_not_touch_order(self):
        llm = FakeLLM([{'content': json.dumps([
            {'n': 0, 'new_comment': '', 'new_order_comment': 'Ира: всё в заказе.',
             'reason': 'перенос'},
            {'n': 1, 'new_comment': '', 'new_order_comment': 'Ира: другой текст.',
             'reason': 'перенос'},
        ], ensure_ascii=False), 'tool_calls': []}])
        docs = [_demand(doc_id='dm1', comment='мусор 1'),
                _demand(doc_id='dm2', comment='мусор 2')]
        out = await review_documents(docs, llm=llm)
        assert out[0]['new_order_comment'] == 'Ира: всё в заказе.'
        assert out[1]['new_order_comment'] is None   # заказ правит только первая


class TestApplyDemand:
    async def test_writes_both_documents(self, monkeypatch):
        calls = []

        async def fake_update(self, session, entity, entity_id, payload):
            calls.append((entity, entity_id, payload))

        from integrations.moysklad_audit import MoySkladAuditClient
        monkeypatch.setattr(MoySkladAuditClient, 'update_entity', fake_update)
        item = {**_demand(comment='мусор'), 'new_comment': '',
                'new_order_comment': 'Оля: забирает для себя. Самовывоз.'}
        await apply_demand(item)
        assert calls == [
            ('demand', 'dm1', {'description': ''}),
            ('customerorder', 'o1', {'description': 'Оля: забирает для себя. Самовывоз.'}),
        ]

    async def test_order_untouched_when_no_order_change(self, monkeypatch):
        calls = []

        async def fake_update(self, session, entity, entity_id, payload):
            calls.append((entity, entity_id, payload))

        from integrations.moysklad_audit import MoySkladAuditClient
        monkeypatch.setattr(MoySkladAuditClient, 'update_entity', fake_update)
        item = {**_demand(), 'new_comment': 'Самовывоз — расходов на доставку нет.',
                'new_order_comment': None}
        await apply_demand(item)
        assert calls == [('demand', 'dm1',
                          {'description': 'Самовывоз — расходов на доставку нет.'})]


class TestDemandGrace:
    def test_fresh_demand_is_too_fresh(self):
        from datetime import datetime
        from services.audit.comment_review import _too_fresh
        assert _too_fresh(datetime.now().strftime('%Y-%m-%d %H:%M:%S')) is True

    def test_old_demand_is_not_fresh(self):
        from services.audit.comment_review import _too_fresh
        assert _too_fresh('2026-05-12 10:00:00') is False

    def test_broken_moment_is_not_fresh(self):
        from services.audit.comment_review import _too_fresh
        assert _too_fresh(None) is False


@pytest.mark.unit
class TestNeedsFinalDot:
    def test_missing_dot_after_name(self):
        assert needs_final_dot('Ира: заказ 52050099-0627-1')

    def test_dot_already_there(self):
        assert not needs_final_dot('Ира: заказ 52050099-0627-1.')

    def test_broken_format_is_not_a_dot_case(self):
        # «Аня готовая пенка» нужна настоящая правка, а не механическая точка
        assert not needs_final_dot('Аня готовая пенка')

    def test_empty_comment(self):
        assert not needs_final_dot('')


@pytest.mark.unit
class TestCollectReportsFailures:
    async def test_failed_entity_reported(self, monkeypatch):
        # МойСклад не ответил по части типов — сбор обязан сказать, по каким именно
        from services.audit import comment_review as cr

        async def boom(self, session, entity, **kw):
            if entity == 'demand':
                raise TimeoutError('Connection timeout')
            return []

        monkeypatch.setattr(cr.MoySkladAuditClient, 'list_entities', boom)
        docs, failed = await cr.collect_documents(30)
        assert docs == []
        assert failed == ['Отгрузка']
