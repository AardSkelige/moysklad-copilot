"""Тесты ревью комментариев: разбор ответа LLM, только suggest попадает в очередь."""

import json

import pytest

from services.audit.comment_review import apply_demand, review_documents

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


def _demand(doc_id='dm1', comment='', order_comment='', overhead=0.0):
    return {'entity': 'demand', 'id': doc_id, 'kind': 'demand',
            'label': 'Отгрузка №00030 от 2026-05-21', 'agent': 'Иванова Мария',
            'sum_rub': 0.0, 'comment': comment, 'overhead_rub': overhead,
            'order_id': 'o1', 'order_name': '00032', 'order_comment': order_comment}


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
