import json
from datetime import datetime, timezone

from sqlalchemy import select

from db.models import AuctionRecord, SpecificationItemRecord
from db.session import SessionLocal
from price_evaluate import (
    EvaluateResult,
    SpecLineForEvaluate,
    evaluate_auction,
    tender_line_total,
)

BATCH_SIZE_DEFAULT = 10


def get_next_yandex_pending_items(
    limit: int = BATCH_SIZE_DEFAULT,
) -> list[tuple[AuctionRecord, SpecificationItemRecord]]:
    with SessionLocal() as session:
        auctions = session.scalars(
            select(AuctionRecord)
            .where(AuctionRecord.detail_status == "done")
            .order_by(AuctionRecord.detail_scraped_at.desc().nulls_last())
        ).all()

        result: list[tuple[AuctionRecord, SpecificationItemRecord]] = []
        expunged_auction_numbers: set[str] = set()

        for auction in auctions:
            items = session.scalars(
                select(SpecificationItemRecord)
                .where(
                    SpecificationItemRecord.auction_number == auction.auction_number,
                    SpecificationItemRecord.yandex_research_status == "pending",
                )
                .order_by(SpecificationItemRecord.position_index)
            ).all()

            for item in items:
                if auction.auction_number not in expunged_auction_numbers:
                    session.expunge(auction)
                    expunged_auction_numbers.add(auction.auction_number)
                session.expunge(item)
                result.append((auction, item))
                if len(result) >= limit:
                    return result

        return result


def _yandex_status_to_evaluate(status: str, median: float | None) -> tuple[str, float | None]:
    if status == "pending":
        return "pending", None
    if status == "not_found" or median is None:
        return "not_found", None
    return "priced", median


def import_yandex_batch(payload: dict) -> tuple[int, list[str]]:
    items_payload = payload.get("items")
    if not isinstance(items_payload, list):
        raise ValueError("В JSON нет массива items")

    analyzed_at_raw = payload.get("analyzed_at")
    if analyzed_at_raw:
        analyzed_at = datetime.fromisoformat(analyzed_at_raw.replace("Z", "+00:00"))
    else:
        analyzed_at = datetime.now(timezone.utc)

    updated_count = 0
    auction_numbers: set[str] = set()

    with SessionLocal() as session:
        for row in items_payload:
            spec_id = row.get("specification_item_id")
            if spec_id is None:
                raise ValueError("В items нет specification_item_id")

            item = session.get(SpecificationItemRecord, int(spec_id))
            if item is None:
                raise ValueError(f"specification_item_id={spec_id} не найден в БД")

            status = row.get("status") or "not_found"
            median = row.get("price_median")
            price_min = row.get("price_min")
            price_max = row.get("price_max")

            if status == "not_found" or median is None:
                item.yandex_research_status = "not_found"
                item.yandex_price_median = None
                item.yandex_price_min = None
                item.yandex_price_max = None
            else:
                item.yandex_research_status = "done"
                item.yandex_price_median = float(median)
                item.yandex_price_min = float(price_min) if price_min is not None else None
                item.yandex_price_max = float(price_max) if price_max is not None else None

            item.yandex_tender_vs_market = row.get("tender_vs_market")
            item.yandex_note = row.get("note")
            cards = row.get("cards")
            if cards is not None:
                item.yandex_cards_json = json.dumps(cards, ensure_ascii=False)
            item.yandex_analyzed_at = analyzed_at

            auction_numbers.add(item.auction_number)
            updated_count += 1

        session.commit()

    return updated_count, sorted(auction_numbers)


def _yandex_lines_for_auction(session, auction_number: str) -> list[SpecLineForEvaluate]:
    items = session.scalars(
        select(SpecificationItemRecord)
        .where(SpecificationItemRecord.auction_number == auction_number)
        .order_by(SpecificationItemRecord.position_index)
    ).all()

    lines: list[SpecLineForEvaluate] = []
    for item in items:
        line_total = tender_line_total(
            float(item.total_price) if item.total_price is not None else None,
            float(item.price_per_unit) if item.price_per_unit is not None else None,
            float(item.quantity) if item.quantity is not None else None,
        )
        eval_status, market_price = _yandex_status_to_evaluate(
            item.yandex_research_status,
            float(item.yandex_price_median) if item.yandex_price_median is not None else None,
        )
        lines.append(
            SpecLineForEvaluate(
                specification_item_id=item.id,
                price_research_status=eval_status,
                tender_line_total=line_total,
                quantity=float(item.quantity) if item.quantity is not None else None,
                market_price_per_unit=market_price,
            )
        )
    return lines


def evaluate_and_save_auction_yandex(auction_number: str) -> EvaluateResult | None:
    with SessionLocal() as session:
        auction = session.get(AuctionRecord, auction_number)
        if auction is None:
            return None

        initial_price = float(auction.initial_price) if auction.initial_price is not None else None
        lines = _yandex_lines_for_auction(session, auction_number)
        result = evaluate_auction(initial_price, lines)

        auction.fit_status_yandex = result.fit_status
        auction.margin_rub_yandex = result.margin_rub
        auction.margin_percent_yandex = result.margin_percent
        auction.rating_yandex = result.rating

        session.commit()
        return result


def evaluate_ready_auctions_yandex(
    auction_numbers: list[str] | None = None,
) -> list[tuple[str, EvaluateResult]]:
    results: list[tuple[str, EvaluateResult]] = []

    with SessionLocal() as session:
        if auction_numbers:
            numbers = auction_numbers
        else:
            numbers = list(
                session.scalars(
                    select(AuctionRecord.auction_number).where(AuctionRecord.detail_status == "done")
                ).all()
            )

    for auction_number in numbers:
        result = evaluate_and_save_auction_yandex(auction_number)
        if result is not None:
            results.append((auction_number, result))

    return results
