"""Тесты учёта показов в ревью комментариев: документ попадается не больше двух раз."""

import pytest

from services.audit import review_tracker

pytestmark = [pytest.mark.asyncio, pytest.mark.unit, pytest.mark.audit]


def _doc(entity='supply', doc_id='d1', comment='аня частичная приемка'):
    return {'entity': entity, 'id': doc_id, 'label': 'Приёмка №00049',
            'comment': comment}


def _demand(comment='', order_comment='', **extra):
    return {'entity': 'demand', 'id': 'dm1', 'kind': 'demand',
            'comment': comment, 'order_comment': order_comment, **extra}


class TestFilterSeen:
    async def test_unseen_doc_passes(self, test_db):
        docs = [_doc()]
        assert await review_tracker.filter_seen(test_db, docs) == docs

    async def test_shown_once_still_passes(self, test_db):
        doc = _doc()
        await review_tracker.mark_shown(test_db, doc)
        assert await review_tracker.filter_seen(test_db, [doc]) == [doc]

    async def test_shown_twice_filtered(self, test_db):
        doc = _doc()
        await review_tracker.mark_shown(test_db, doc)
        await review_tracker.mark_shown(test_db, doc)
        assert await review_tracker.filter_seen(test_db, [doc]) == []

    async def test_changed_comment_resets_counter(self, test_db):
        doc = _doc()
        await review_tracker.mark_shown(test_db, doc)
        await review_tracker.mark_shown(test_db, doc)
        edited = _doc(comment='Аня: частичная приёмка. Дописали причину.')
        # изменённый руками документ снова допускается к ревью
        assert await review_tracker.filter_seen(test_db, [edited]) == [edited]
        await review_tracker.mark_shown(test_db, edited)
        # счётчик пошёл заново: после одного показа документ ещё не отфильтрован
        assert await review_tracker.filter_seen(test_db, [edited]) == [edited]

    async def test_demand_state_includes_order_comment(self, test_db):
        d = _demand(comment='Накладные расходы 0 — самовывоз.', order_comment='Оля: ок.')
        await review_tracker.mark_shown(test_db, d)
        await review_tracker.mark_shown(test_db, d)
        assert await review_tracker.filter_seen(test_db, [d]) == []
        edited = _demand(comment='Накладные расходы 0 — самовывоз.',
                         order_comment='Оля: ок. Трек — 123.')
        assert await review_tracker.filter_seen(test_db, [edited]) == [edited]


class TestRecordApplied:
    async def test_bot_edit_does_not_reset_counter(self, test_db):
        doc = _doc(comment='аня частичная приемка')
        await review_tracker.mark_shown(test_db, doc)
        applied = {**doc, 'new_comment': 'Аня: частичная приёмка.'}
        await review_tracker.record_applied(test_db, applied)
        # следующий прогон видит уже новый текст — счётчик не обнулился
        after = _doc(comment='Аня: частичная приёмка.')
        await review_tracker.mark_shown(test_db, after)
        assert await review_tracker.filter_seen(test_db, [after]) == []

    async def test_applied_demand_clears_comment(self, test_db):
        d = _demand(comment='мусор', order_comment='Оля: ок.')
        await review_tracker.mark_shown(test_db, d)
        applied = {**d, 'new_comment': '', 'new_order_comment': 'Оля: ок. Самовывоз.'}
        await review_tracker.record_applied(test_db, applied)
        after = _demand(comment='', order_comment='Оля: ок. Самовывоз.')
        await review_tracker.mark_shown(test_db, after)
        assert await review_tracker.filter_seen(test_db, [after]) == []
