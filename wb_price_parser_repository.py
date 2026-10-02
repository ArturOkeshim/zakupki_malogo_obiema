from datetime import datetime, timezone

from sqlalchemy import select

from db.models import AuctionRecord, SpecificationItemRecord, WbOfferRecord
from db.session import SessionLocal
from wb_price_parser_schemas import WbProductCard


def get_auctions_batch(limit: int, include_failed: bool = False) -> list[AuctionRecord]:
    """
    Аукционы для парсинга WB.
    По умолчанию — только analysis_status=pending.
    include_failed=True — ещё и failed (для повторного запуска).
    """
    statuses = ["pending"]
    if include_failed:
        statuses.append("failed")

    with SessionLocal() as session:
        auctions = session.scalars(
            select(AuctionRecord)
            .where(AuctionRecord.detail_status == "done")
            .where(AuctionRecord.analysis_status.in_(statuses))
            .limit(limit)
        ).all()

        for auction in auctions:
            session.expunge(auction)

        return list(auctions)


def get_pending_auctions_batch(limit: int) -> list[AuctionRecord]:
    return get_auctions_batch(limit, include_failed=False)


def get_specification_items(auction_number: str) -> list[SpecificationItemRecord]:
    with SessionLocal() as session:
        items = session.scalars(
            select(SpecificationItemRecord)
            .where(SpecificationItemRecord.auction_number == auction_number)
            .order_by(SpecificationItemRecord.position_index)
        ).all()

        for item in items:
            session.expunge(item)

        return list(items)


def save_wb_offers(
    specification_item_id: int,
    auction_number: str,
    search_query: str,
    cards: list[WbProductCard],
) -> int:
    """
    Сохраняет карточки WB для одной позиции спецификации.
    Старые карточки по этой позиции удаляются — всегда храним последний поиск.
    """
    if not cards:
        return 0

    scraped_at = datetime.now(timezone.utc)

    with SessionLocal() as session:
        session.query(WbOfferRecord).filter_by(
            specification_item_id=specification_item_id
        ).delete()

        for index, card in enumerate(cards, start=1):
            session.add(
                WbOfferRecord(
                    specification_item_id=specification_item_id,
                    auction_number=auction_number,
                    search_query=search_query,
                    result_rank=index,
                    wb_name=card.name,
                    wb_price=card.price,
                    wb_old_price=card.old_price,
                    wb_url=card.url,
                    match_status="pending",
                    scraped_at=scraped_at,
                )
            )

        session.commit()

    return len(cards)


def save_analysis_status(
    auction_number: str,
    analysis_status: str,
    error_message: str | None = None,
) -> None:
    with SessionLocal() as session:
        auction = session.get(AuctionRecord, auction_number)
        if auction is None:
            return

        auction.analysis_status = analysis_status
        if analysis_status == "done":
            auction.error_message = None
        elif error_message is not None:
            auction.error_message = error_message

        session.commit()
