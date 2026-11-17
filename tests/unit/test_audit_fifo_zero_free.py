import pytest

from services.audit.checks.products import _explained_as_free, says_free


def _src(price, comment):
    return {'price_kopecks': price, 'comment': comment}


@pytest.mark.unit
class TestZeroExplainedByZeroDocsOnly:
    def test_paid_doc_comment_does_not_spoil_free_zero(self):
        sources = [_src(48, 'Яна: по нулям.'), _src(0, 'Яна бесплатно')]
        assert _explained_as_free(sources, None) is True

    def test_paid_last_supply_comment_is_ignored(self):
        sources = [_src(0, 'Яна бесплатно')]
        assert _explained_as_free(sources, _src(47, 'Яна: купила на свои')) is True

    def test_zero_doc_without_reason_is_flagged(self):
        sources = [_src(48, 'бесплатно'), _src(0, 'Яна: пересчёт')]
        assert _explained_as_free(sources, None) is False


@pytest.mark.unit
class TestZeroPriceStatedInComment:
    def test_price_zero_with_reason_counts_as_explained(self):
        assert says_free('Серёжа: пересорт. Цена 0: себестоимость списанных 0 ₽.')

    def test_nonzero_price_starting_with_zero_is_not_free(self):
        assert not says_free('Цена 0,48 ₽ внесена по ошибке')
        assert not says_free('цена 0.8')
