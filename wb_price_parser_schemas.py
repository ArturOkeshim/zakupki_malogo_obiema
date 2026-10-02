from dataclasses import dataclass


@dataclass
class WbProductCard:
    """Одна карточка из выдачи Wildberries."""
    name: str
    price: float | None
    old_price: float | None
    url: str
