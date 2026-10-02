from dataclasses import dataclass, field


@dataclass
class SpecificationItem:
    position_index: int
    name: str
    sku_url: str | None
    image_url: str | None
    quantity_text: str | None
    quantity: float | None
    unit: str | None
    price_per_unit: float | None
    total_price: float | None


@dataclass
class DeliveryStageRow:
    customer_name: str | None
    name: str | None
    price_per_unit: float | None
    quantity: float | None
    total_price: float | None


@dataclass
class DeliveryStage:
    stage_number: int
    stage_price: float | None
    address: str | None
    date_range: str | None
    rows: list[DeliveryStageRow] = field(default_factory=list)


@dataclass
class AuctionDetail:
    specification_items: list[SpecificationItem] = field(default_factory=list)
    delivery_stages: list[DeliveryStage] = field(default_factory=list)
