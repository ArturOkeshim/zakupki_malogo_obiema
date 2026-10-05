import json
from datetime import datetime, timezone

from sqlalchemy import select

from db.models import AuctionRecord, PurchaseRecord, SpecificationItemRecord
from db.session import SessionLocal
from price_evaluate import (
    EvaluateResult,
    SpecLineForEvaluate,
    evaluate_auction,
    tender_line_total,
)

# Одна позиция на запуск агента: чужие строки в том же промпте сбивают поиск.
BATCH_SIZE_DEFAULT = 1


def get_unfinished_auction_numbers() -> list[str]:
    """
    Закупки, которые пайплайн уже сохранил, но не довёл до цены.
    Это нет спецификации или есть позиции со статусом pending.
    Уже оценённые закупки сюда не входят.
    """
    with SessionLocal() as session:
        purchases = session.scalars(
            select(PurchaseRecord).order_by(PurchaseRecord.scraped_at)
        ).all()

        latest_scraped_at = {}
        for purchase in purchases:
            latest_scraped_at[purchase.auction_number] = purchase.scraped_at

        done_numbers = set(
            session.scalars(
                select(AuctionRecord.auction_number).where(AuctionRecord.detail_status == "done")
            ).all()
        )
        pending_numbers = set(
            session.scalars(
                select(SpecificationItemRecord.auction_number)
                .where(SpecificationItemRecord.price_research_status == "pending")
                .distinct()
            ).all()
        )

    unfinished = []
    for auction_number, scraped_at in latest_scraped_at.items():
        detail_missing = auction_number not in done_numbers
        price_missing = auction_number in pending_numbers
        if detail_missing or price_missing:
            unfinished.append((scraped_at, auction_number))

    unfinished.sort()
    return [auction_number for _, auction_number in unfinished]


def numbers_missing_detail(auction_numbers: list[str]) -> list[str]:
    """Из списка оставляет номера, у которых спецификация ещё не снята."""
    if not auction_numbers:
        return []

    with SessionLocal() as session:
        done_numbers = set(
            session.scalars(
                select(AuctionRecord.auction_number).where(
                    AuctionRecord.auction_number.in_(auction_numbers),
                    AuctionRecord.detail_status == "done",
                )
            ).all()
        )

    return [number for number in auction_numbers if number not in done_numbers]


def get_next_pending_items(
    limit: int = BATCH_SIZE_DEFAULT,
    auction_numbers: list[str] | None = None,
) -> list[tuple[AuctionRecord, SpecificationItemRecord]]:
    """
    Следующие limit позиций со status=pending.
    Сначала тендеры с detail_status=done (свежее detail_scraped_at — раньше в очереди).
    """
    with SessionLocal() as session:
        auctions = session.scalars(
            select(AuctionRecord)
            .where(AuctionRecord.detail_status == "done")
            .order_by(AuctionRecord.detail_scraped_at.desc().nulls_last())
        ).all()
        if auction_numbers is not None:
            allowed = set(auction_numbers)
            auctions = [auction for auction in auctions if auction.auction_number in allowed]

        result: list[tuple[AuctionRecord, SpecificationItemRecord]] = []
        # ЗАМЕТКА ДЛЯ ОБУЧЕНИЯ: один тендер может дать несколько позиций в батче.
        # expunge для одного и того же auction делаем только один раз.
        expunged_auction_numbers: set[str] = set()

        for auction in auctions:
            items = session.scalars(
                select(SpecificationItemRecord)
                .where(
                    SpecificationItemRecord.auction_number == auction.auction_number,
                    SpecificationItemRecord.price_research_status == "pending",
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


def import_price_research_batch(payload: dict) -> tuple[int, list[str]]:
    """
    Сохраняет ответ нейросети. Возвращает (число обновлённых строк, auction_number для пересчёта).
    """
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

            confidence = row.get("confidence") or "not_found"
            market_price = row.get("market_price_per_unit")

            if confidence == "not_found" or market_price is None:
                item.price_research_status = "not_found"
                item.market_price_per_unit = None
            else:
                item.price_research_status = "priced"
                item.market_price_per_unit = float(market_price)

            item.market_confidence = confidence
            item.market_item_type = row.get("item_type")
            item.market_note = row.get("note")
            sources = row.get("sources")
            if sources is not None:
                item.market_sources_json = json.dumps(sources, ensure_ascii=False)
            item.market_analyzed_at = analyzed_at

            auction_numbers.add(item.auction_number)
            updated_count += 1

        session.commit()

    return updated_count, sorted(auction_numbers)


def _lines_for_auction(session, auction_number: str) -> list[SpecLineForEvaluate]:
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
        lines.append(
            SpecLineForEvaluate(
                specification_item_id=item.id,
                price_research_status=item.price_research_status,
                tender_line_total=line_total,
                quantity=float(item.quantity) if item.quantity is not None else None,
                market_price_per_unit=(
                    float(item.market_price_per_unit)
                    if item.market_price_per_unit is not None
                    else None
                ),
            )
        )
    return lines


def evaluate_and_save_auction(auction_number: str) -> EvaluateResult | None:
    with SessionLocal() as session:
        auction = session.get(AuctionRecord, auction_number)
        if auction is None:
            return None

        initial_price = float(auction.initial_price) if auction.initial_price is not None else None
        lines = _lines_for_auction(session, auction_number)
        result = evaluate_auction(initial_price, lines)

        auction.fit_status = result.fit_status
        auction.margin_rub = result.margin_rub
        auction.margin_percent = result.margin_percent
        auction.rating = result.rating

        if result.pending_count == 0 and lines:
            auction.analysis_status = "done"

        session.commit()
        return result


def evaluate_ready_auctions(auction_numbers: list[str] | None = None) -> list[tuple[str, EvaluateResult]]:
    """
    Пересчёт fit_status. Если auction_numbers не передан — все detail_status=done.
    """
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
        result = evaluate_and_save_auction(auction_number)
        if result is not None:
            results.append((auction_number, result))

    return results
