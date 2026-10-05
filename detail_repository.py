import json
from datetime import datetime, timezone

from sqlalchemy import select

from db.models import (
    AuctionRecord,
    DeliveryStageRecord,
    DeliveryStageRowRecord,
    PurchaseRecord,
    SpecificationItemRecord,
)
from db.session import SessionLocal
from detail_schemas import AuctionDetail, DeliveryStage, SpecificationItem
from parsing import MOSCOW_TZ, parse_deadline_from_date_range


def get_active_purchases_for_detail() -> list[PurchaseRecord]:
    """
    Все активные закупки для detail.py, отсортированные по дедлайну (дальний первым).
    """
    now_moscow = datetime.now(MOSCOW_TZ)

    with SessionLocal() as session:
        done_auctions = select(AuctionRecord.auction_number).where(
            AuctionRecord.detail_status == "done"
        )

        purchases = session.scalars(
            select(PurchaseRecord).where(PurchaseRecord.auction_number.not_in(done_auctions))
        ).all()

        if not purchases:
            return []

        latest_purchase_by_auction: dict[str, PurchaseRecord] = {}
        for purchase in purchases:
            current = latest_purchase_by_auction.get(purchase.auction_number)
            if current is None or purchase.scraped_at > current.scraped_at:
                latest_purchase_by_auction[purchase.auction_number] = purchase

        active_purchases: list[tuple[datetime, PurchaseRecord]] = []
        for purchase in latest_purchase_by_auction.values():
            deadline = parse_deadline_from_date_range(purchase.date_range)
            if deadline is None:
                continue
            if deadline <= now_moscow:
                continue
            active_purchases.append((deadline, purchase))

        active_purchases.sort(key=lambda item: (item[0], item[1].scraped_at), reverse=True)

        result = [purchase for _, purchase in active_purchases]
        for purchase in result:
            session.expunge(purchase)
        return result


def get_purchases_by_numbers(auction_numbers: list[str]) -> list[PurchaseRecord]:
    """Последняя сохранённая карточка для каждого номера, в том же порядке, что в списке."""
    with SessionLocal() as session:
        purchases = session.scalars(
            select(PurchaseRecord).where(PurchaseRecord.auction_number.in_(auction_numbers))
        ).all()

        latest_by_number: dict[str, PurchaseRecord] = {}
        for purchase in purchases:
            current = latest_by_number.get(purchase.auction_number)
            if current is None or purchase.scraped_at > current.scraped_at:
                latest_by_number[purchase.auction_number] = purchase

        result = []
        for auction_number in auction_numbers:
            purchase = latest_by_number.get(auction_number)
            if purchase is None:
                continue
            session.expunge(purchase)
            result.append(purchase)
        return result


def get_active_purchases_batch(limit: int) -> list[PurchaseRecord]:
    """Первые limit закупок из актуальной очереди (перечитывается из БД при каждом вызове)."""
    return get_active_purchases_for_detail()[:limit]


def save_auction_detail(
    purchase: PurchaseRecord,
    detail: AuctionDetail,
    error_message: str | None = None,
) -> None:
    detail_status = "failed" if error_message else "done"
    scraped_at = datetime.now(timezone.utc)

    with SessionLocal() as session:
        auction = session.get(AuctionRecord, purchase.auction_number)
        if auction is None:
            auction = AuctionRecord(
                auction_number=purchase.auction_number,
                detail_url=purchase.detail_url,
                name=purchase.name,
                initial_price=purchase.initial_price,
                customer=purchase.customer,
                status=purchase.status,
            )
            session.add(auction)

        auction.source_purchase_id = purchase.id
        auction.detail_url = purchase.detail_url
        auction.name = purchase.name
        auction.initial_price = purchase.initial_price
        auction.customer = purchase.customer
        auction.status = purchase.status
        auction.detail_status = detail_status
        auction.error_message = error_message
        auction.detail_scraped_at = scraped_at if detail_status == "done" else auction.detail_scraped_at

        if detail_status == "done":
            session.query(SpecificationItemRecord).filter_by(
                auction_number=purchase.auction_number
            ).delete()
            session.query(DeliveryStageRecord).filter_by(
                auction_number=purchase.auction_number
            ).delete()

            for item in detail.specification_items:
                session.add(
                    SpecificationItemRecord(
                        auction_number=purchase.auction_number,
                        position_index=item.position_index,
                        name=item.name,
                        sku_url=item.sku_url,
                        image_url=item.image_url,
                        quantity_text=item.quantity_text,
                        quantity=item.quantity,
                        unit=item.unit,
                        price_per_unit=item.price_per_unit,
                        total_price=item.total_price,
                    )
                )

            for stage in detail.delivery_stages:
                stage_record = DeliveryStageRecord(
                    auction_number=purchase.auction_number,
                    stage_number=stage.stage_number,
                    stage_price=stage.stage_price,
                    address=stage.address,
                    date_range=stage.date_range,
                )
                session.add(stage_record)
                session.flush()

                for row in stage.rows:
                    session.add(
                        DeliveryStageRowRecord(
                            delivery_stage_id=stage_record.id,
                            customer_name=row.customer_name,
                            name=row.name,
                            price_per_unit=row.price_per_unit,
                            quantity=row.quantity,
                            total_price=row.total_price,
                        )
                    )

        session.commit()


def get_specification_items_by_auctions(auction_numbers: list[str]) -> list[SpecificationItemRecord]:
    """Позиции спецификации этих закупок: сначала порядок номеров, внутри — position_index."""
    with SessionLocal() as session:
        items = session.scalars(
            select(SpecificationItemRecord).where(
                SpecificationItemRecord.auction_number.in_(auction_numbers)
            )
        ).all()

        auction_order = {number: index for index, number in enumerate(auction_numbers)}
        sorted_items = sorted(
            items,
            key=lambda item: (auction_order.get(item.auction_number, 10**9), item.position_index),
        )
        for item in sorted_items:
            session.expunge(item)
        return sorted_items


def save_sku_card_details(
    specification_item_id: int,
    model: str | None,
    manufacturer: str | None,
    characteristics: list[dict],
) -> None:
    """Пишет модель, производителя и характеристики. sku_scraped_at отмечает, что страница открылась."""
    with SessionLocal() as session:
        item = session.get(SpecificationItemRecord, specification_item_id)
        if item is None:
            raise ValueError(f"specification_item_id={specification_item_id} не найден в БД")

        item.sku_model = model
        item.sku_manufacturer = manufacturer
        item.sku_characteristics_json = json.dumps(characteristics, ensure_ascii=False)
        item.sku_scraped_at = datetime.now(timezone.utc)
        session.commit()
