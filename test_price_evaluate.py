"""Проверка формул выгодности без БД."""

from price_evaluate import SpecLineForEvaluate, evaluate_auction


def test_profitable_with_min_revenue():
    lines = [
        SpecLineForEvaluate(1, "priced", 100_000, 10, 5_000),
        SpecLineForEvaluate(2, "not_found", 50_000, 1, None),
    ]
    result = evaluate_auction(initial_price=200_000, lines=lines)
    assert result.revenue == 150_000
    assert result.estimated_cost == 50_000 + 50_000 * 1.10
    assert result.margin_rub == 150_000 - (50_000 + 55_000)
    assert result.fit_status == "profitable"


def test_service_without_quantity_uses_one_unit():
    lines = [
        SpecLineForEvaluate(1, "priced", 6_000, None, 5_500),
    ]
    result = evaluate_auction(initial_price=10_000, lines=lines)
    assert result.revenue == 6_000
    assert result.estimated_cost == 5_500
    assert result.fit_status == "unprofitable"


def test_unprofitable_below_20k():
    lines = [
        SpecLineForEvaluate(1, "priced", 100_000, 1, 95_000),
    ]
    result = evaluate_auction(initial_price=100_000, lines=lines)
    assert result.margin_rub == 5_000
    assert result.fit_status == "unprofitable"


if __name__ == "__main__":
    test_profitable_with_min_revenue()
    test_service_without_quantity_uses_one_unit()
    test_unprofitable_below_20k()
    print("OK")
