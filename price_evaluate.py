"""
Расчёт выгодности тендера (правила v1).

ЗАМЕТКА ДЛЯ ОБУЧЕНИЯ:
Чистые функции без БД — проще проверять логику и читать формулы.
"""

from dataclasses import dataclass

MIN_MARGIN_RUB = 20_000
NOT_FOUND_COST_MULTIPLIER = 1.10


@dataclass
class SpecLineForEvaluate:
    specification_item_id: int
    price_research_status: str
    tender_line_total: float | None
    quantity: float | None
    market_price_per_unit: float | None


@dataclass
class EvaluateResult:
    fit_status: str
    revenue: float | None
    spec_sum: float
    initial_price: float | None
    estimated_cost: float | None
    margin_rub: float | None
    margin_percent: float | None
    rating: int | None
    pending_count: int
    not_found_count: int


def tender_line_total(
    total_price: float | None,
    price_per_unit: float | None,
    quantity: float | None,
) -> float | None:
    if total_price is not None:
        return float(total_price)

    if price_per_unit is not None and quantity is not None:
        return float(price_per_unit) * float(quantity)

    # Услуги: часто есть только «цена за ед.» без количества — считаем как 1 ед.
    if price_per_unit is not None:
        return float(price_per_unit)

    return None


def line_cost_for_item(
    price_research_status: str,
    tender_line_total_value: float | None,
    quantity: float | None,
    market_price_per_unit: float | None,
) -> float | None:
    if price_research_status == "pending":
        return None

    if price_research_status == "not_found":
        if tender_line_total_value is None:
            return None
        return tender_line_total_value * NOT_FOUND_COST_MULTIPLIER

    if market_price_per_unit is None:
        return None

    if quantity is not None:
        return float(market_price_per_unit) * float(quantity)

    return float(market_price_per_unit)


def rating_from_margin_percent(margin_percent: float | None, margin_rub: float | None) -> int | None:
    if margin_percent is None:
        return None

    if margin_rub is not None and margin_rub < 0:
        return 1

    if margin_percent < 5:
        return 1
    if margin_percent < 10:
        return 2
    if margin_percent < 20:
        return 3
    if margin_percent < 35:
        return 4
    return 5


def evaluate_auction(
    initial_price: float | None,
    lines: list[SpecLineForEvaluate],
) -> EvaluateResult:
    pending_count = sum(1 for line in lines if line.price_research_status == "pending")
    not_found_count = sum(1 for line in lines if line.price_research_status == "not_found")

    spec_sum = 0.0
    for line in lines:
        if line.tender_line_total is not None:
            spec_sum += line.tender_line_total

    if pending_count > 0:
        return EvaluateResult(
            fit_status="unknown",
            revenue=None,
            spec_sum=spec_sum,
            initial_price=initial_price,
            estimated_cost=None,
            margin_rub=None,
            margin_percent=None,
            rating=None,
            pending_count=pending_count,
            not_found_count=not_found_count,
        )

    if not lines:
        return EvaluateResult(
            fit_status="unknown",
            revenue=None,
            spec_sum=0.0,
            initial_price=initial_price,
            estimated_cost=None,
            margin_rub=None,
            margin_percent=None,
            rating=None,
            pending_count=0,
            not_found_count=0,
        )

    revenue_candidates = []
    if initial_price is not None:
        revenue_candidates.append(float(initial_price))
    if spec_sum > 0:
        revenue_candidates.append(spec_sum)

    if not revenue_candidates:
        return EvaluateResult(
            fit_status="unknown",
            revenue=None,
            spec_sum=spec_sum,
            initial_price=initial_price,
            estimated_cost=None,
            margin_rub=None,
            margin_percent=None,
            rating=None,
            pending_count=0,
            not_found_count=not_found_count,
        )

    revenue = min(revenue_candidates)

    estimated_cost = 0.0
    for line in lines:
        line_cost = line_cost_for_item(
            line.price_research_status,
            line.tender_line_total,
            line.quantity,
            line.market_price_per_unit,
        )
        if line_cost is None:
            return EvaluateResult(
                fit_status="unknown",
                revenue=revenue,
                spec_sum=spec_sum,
                initial_price=initial_price,
                estimated_cost=None,
                margin_rub=None,
                margin_percent=None,
                rating=None,
                pending_count=0,
                not_found_count=not_found_count,
            )
        estimated_cost += line_cost

    margin_rub = revenue - estimated_cost
    margin_percent = (margin_rub / revenue) * 100 if revenue > 0 else None

    if margin_rub >= MIN_MARGIN_RUB:
        fit_status = "profitable"
    else:
        fit_status = "unprofitable"

    rating = rating_from_margin_percent(margin_percent, margin_rub)

    return EvaluateResult(
        fit_status=fit_status,
        revenue=revenue,
        spec_sum=spec_sum,
        initial_price=initial_price,
        estimated_cost=estimated_cost,
        margin_rub=margin_rub,
        margin_percent=margin_percent,
        rating=rating,
        pending_count=0,
        not_found_count=not_found_count,
    )
