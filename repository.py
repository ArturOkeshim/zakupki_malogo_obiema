from db.models import PurchaseRecord
from db.session import SessionLocal
from schemas import Purchase


def save_purchases(purchases: list[Purchase]) -> int:
    """Добавляет закупки в Postgres. Существующие строки не трогает."""
    if not purchases:
        return 0

    records = [PurchaseRecord(**purchase.to_db_dict()) for purchase in purchases]

    with SessionLocal() as session:
        session.add_all(records)
        session.commit()

    return len(records)
