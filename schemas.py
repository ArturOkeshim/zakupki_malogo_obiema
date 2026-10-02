from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any


@dataclass
class Purchase:
    """Данные одной закупки после парсинга (до записи в БД)."""

    auction_number: str
    name: str
    initial_price: float | None
    customer: str
    status: str
    purchase_type: str
    region: str
    law: str
    date_range: str
    integration: str
    detail_url: str
    source_page: int
    scraped_at: datetime

    def to_db_dict(self) -> dict[str, Any]:
        return asdict(self)
