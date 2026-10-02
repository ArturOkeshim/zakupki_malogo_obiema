from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class PurchaseRecord(Base):
    __tablename__ = "purchases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    auction_number: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    initial_price: Mapped[float | None] = mapped_column(Numeric(15, 2), nullable=True)
    customer: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str | None] = mapped_column(String(100), nullable=True)
    purchase_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    region: Mapped[str | None] = mapped_column(String(200), nullable=True)
    law: Mapped[str | None] = mapped_column(String(50), nullable=True)
    date_range: Mapped[str | None] = mapped_column(String(200), nullable=True)
    integration: Mapped[str | None] = mapped_column(String(200), nullable=True)
    detail_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scraped_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class AuctionRecord(Base):
    __tablename__ = "auctions"

    auction_number: Mapped[str] = mapped_column(String(50), primary_key=True)
    source_purchase_id: Mapped[int | None] = mapped_column(ForeignKey("purchases.id"), nullable=True)
    detail_url: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    initial_price: Mapped[float | None] = mapped_column(Numeric(15, 2), nullable=True)
    customer: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str | None] = mapped_column(String(100), nullable=True)
    detail_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    analysis_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    fit_status: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    margin_rub: Mapped[float | None] = mapped_column(Numeric(15, 2), nullable=True)
    margin_percent: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fit_status_yandex: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    margin_rub_yandex: Mapped[float | None] = mapped_column(Numeric(15, 2), nullable=True)
    margin_percent_yandex: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    rating_yandex: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail_scraped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SpecificationItemRecord(Base):
    __tablename__ = "specification_items"
    __table_args__ = (
        UniqueConstraint("auction_number", "position_index", name="uq_spec_item_position"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    auction_number: Mapped[str] = mapped_column(
        ForeignKey("auctions.auction_number", ondelete="CASCADE"),
        nullable=False,
    )
    position_index: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    sku_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    quantity_text: Mapped[str | None] = mapped_column(String(100), nullable=True)
    quantity: Mapped[float | None] = mapped_column(Numeric(15, 3), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(50), nullable=True)
    price_per_unit: Mapped[float | None] = mapped_column(Numeric(15, 2), nullable=True)
    total_price: Mapped[float | None] = mapped_column(Numeric(15, 2), nullable=True)
    price_research_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending"
    )
    market_price_per_unit: Mapped[float | None] = mapped_column(Numeric(15, 2), nullable=True)
    market_confidence: Mapped[str | None] = mapped_column(String(20), nullable=True)
    market_item_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    market_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    market_sources_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    market_analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    yandex_research_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending"
    )
    yandex_price_min: Mapped[float | None] = mapped_column(Numeric(15, 2), nullable=True)
    yandex_price_max: Mapped[float | None] = mapped_column(Numeric(15, 2), nullable=True)
    yandex_price_median: Mapped[float | None] = mapped_column(Numeric(15, 2), nullable=True)
    yandex_tender_vs_market: Mapped[str | None] = mapped_column(String(30), nullable=True)
    yandex_cards_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    yandex_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    yandex_analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DeliveryStageRecord(Base):
    __tablename__ = "delivery_stages"
    __table_args__ = (
        UniqueConstraint("auction_number", "stage_number", name="uq_delivery_stage_number"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    auction_number: Mapped[str] = mapped_column(
        ForeignKey("auctions.auction_number", ondelete="CASCADE"),
        nullable=False,
    )
    stage_number: Mapped[int] = mapped_column(Integer, nullable=False)
    stage_price: Mapped[float | None] = mapped_column(Numeric(15, 2), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    date_range: Mapped[str | None] = mapped_column(String(200), nullable=True)


class DeliveryStageRowRecord(Base):
    __tablename__ = "delivery_stage_rows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    delivery_stage_id: Mapped[int] = mapped_column(
        ForeignKey("delivery_stages.id", ondelete="CASCADE"),
        nullable=False,
    )
    customer_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    price_per_unit: Mapped[float | None] = mapped_column(Numeric(15, 2), nullable=True)
    quantity: Mapped[float | None] = mapped_column(Numeric(15, 3), nullable=True)
    total_price: Mapped[float | None] = mapped_column(Numeric(15, 2), nullable=True)


class WbOfferRecord(Base):
    """
    Одна карточка из выдачи Wildberries по позиции спецификации.
    match_status = pending — ждёт проверки нейросетью на следующем этапе.
    """
    __tablename__ = "wb_offers"
    __table_args__ = (
        UniqueConstraint(
            "specification_item_id",
            "result_rank",
            name="uq_wb_offer_spec_item_rank",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    specification_item_id: Mapped[int] = mapped_column(
        ForeignKey("specification_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    auction_number: Mapped[str] = mapped_column(String(50), nullable=False)
    search_query: Mapped[str] = mapped_column(Text, nullable=False)
    result_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    wb_name: Mapped[str] = mapped_column(Text, nullable=False)
    wb_price: Mapped[float | None] = mapped_column(Numeric(15, 2), nullable=True)
    wb_old_price: Mapped[float | None] = mapped_column(Numeric(15, 2), nullable=True)
    wb_url: Mapped[str] = mapped_column(Text, nullable=False)
    # pending → matched / rejected (заполнит следующий этап с нейросетью)
    match_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    scraped_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
