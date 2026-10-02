from sqlalchemy import select

from db.models import PurchaseRecord
from db.session import SessionLocal
from schemas import Purchase


def existing_auction_numbers() -> set[str]:
    """Номера закупок, которые уже лежат в таблице purchases."""
    with SessionLocal() as session:
        numbers = session.scalars(select(PurchaseRecord.auction_number)).all()
    return set(numbers)


def save_purchases(purchases: list[Purchase]) -> int:
    """Добавляет закупки в Postgres. Существующие строки не трогает."""
    if not purchases:
        return 0

    records = [PurchaseRecord(**purchase.to_db_dict()) for purchase in purchases]

    with SessionLocal() as session:
        session.add_all(records)
        session.commit()

    return len(records)
