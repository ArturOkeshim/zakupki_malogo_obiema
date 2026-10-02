import re
from datetime import datetime, timedelta, timezone

# МСК = UTC+3 (без пакета tzdata на Windows)
MOSCOW_TZ = timezone(timedelta(hours=3))


def parse_deadline_from_date_range(date_range: str | None) -> datetime | None:
    """
    Достаёт дедлайн из date_range.
    Пример: «с 13.08.2026 до 14.08.2026 15:11 (МСК)» → 14.08.2026 15:11 МСК
    """
    if not date_range:
        return None

    match = re.search(
        r"до\s+(\d{2}\.\d{2}\.\d{4})(?:\s+(\d{2}:\d{2}))?",
        date_range,
    )
    if not match:
        return None

    date_part = match.group(1)
    time_part = match.group(2) or "23:59"

    deadline = datetime.strptime(f"{date_part} {time_part}", "%d.%m.%Y %H:%M")
    return deadline.replace(tzinfo=MOSCOW_TZ)


def price_to_number(price_text: str) -> float | None:
    if not price_text:
        return None

    cleaned = price_text.replace("₽", "").replace("\xa0", " ")
    cleaned = cleaned.replace(" ", "").strip()
    cleaned = cleaned.replace(",", ".")

    if not cleaned:
        return None

    return float(cleaned)


def parse_quantity(quantity_text: str) -> tuple[float | None, str | None]:
    if not quantity_text:
        return None, None

    match = re.match(r"^([\d.,]+)\s*(.*)$", quantity_text.strip())
    if not match:
        return None, quantity_text.strip() or None

    quantity = float(match.group(1).replace(",", "."))
    unit = match.group(2).strip() or None
    return quantity, unit
