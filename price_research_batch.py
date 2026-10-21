"""Общая сборка INPUT для export (Claude и Yandex)."""

import json
import re
import urllib.parse
from datetime import datetime, timezone

SEARCH_KIND_RETAIL = "retail"
SEARCH_KIND_SERVICE = "service"
SEARCH_KIND_UNCLEAR = "unclear"

MARKET_SEARCH_URL = "https://market.yandex.ru/search?text="
WEB_SEARCH_URL = "https://yandex.ru/search/?text="

SERVICE_NAME_MARKERS = (
    "услуг",
    "страхован",
    "разработка проект",
    "проектной документац",
    "проектную документац",
    "проектно-сметн",
    "обеспечение сервисом",
    "обеспечению сервисом",
    "техническое обслуживан",
    "техобслуживан",
    "обслуживан",
    "лиценз",
    "работы по",
    "изготовлению",
    "изготовление ",
    "программн",
    "smeta",
    "смета.ру",
    "смета.ru",
    "аутсорсинг",
    "консультац",
    "экспертиз",
    "проектирован",
    "авторск",
    "выполнение работ",
    "производство работ",
    "пусконалад",
    "сопровожден",
    "абонентск",
    "подписк",
    "лизинг",
    "клининг",
    "охрана ",
    "охраны ",
    "охрану ",
    "перевозк",
    "грузоперевоз",
)
SERVICE_UNITS = (
    "усл",
    "услуг",
    "человеко",
    "чел.",
    "чел/",
    "нормо",
)
RETAIL_UNITS = (
    "шт",
    "штук",
    "упак",
    "комплект",
    "кг",
    "г",
    "л",
    "мл",
    "м",
    "м2",
    "м3",
    "пара",
    "набор",
)
LEGAL_NAME_MARKERS = (
    "ооо",
    "оао",
    "зао",
    "пао",
    "ао ",
    "ао«",
    "ао\"",
    "ип ",
    "ип.",
    "ип«",
    "ип\"",
    "общество с ограничен",
    "ltd",
    "llc",
    "gmbh",
)
COMMODITY_COLOR_MARKERS = (
    "штукатурк",
    "шпаклев",
    "шпатлев",
    "клей",
    "пескобетон",
    "цемент",
    "грунтовк",
    "смесь",
    "смеси",
)
SIZE_HINTS = (
    "размер",
    "габарит",
    "ширин",
    "высот",
    "длин",
    "диаметр",
    "площад",
    " мм",
    " см",
    "м²",
    "м2",
    "×",
    " x ",
)


def _norm(value) -> str:
    return str(value or "").strip().lower()


def classify_search_kind(spec_item) -> str:
    """retail — сначала Маркет; service/unclear — сразу Search API (на Маркете нет)."""
    name = _norm(getattr(spec_item, "name", None))
    unit = _norm(getattr(spec_item, "unit", None))
    unit_first = unit.replace(".", " ").split()[0] if unit else ""
    model = _norm(getattr(spec_item, "sku_model", None))
    manufacturer = _norm(getattr(spec_item, "sku_manufacturer", None))
    has_sku = bool(model or manufacturer)

    service_by_name = any(marker in name for marker in SERVICE_NAME_MARKERS)
    service_by_unit = any(marker in unit for marker in SERVICE_UNITS)
    retail_by_unit = unit_first in RETAIL_UNITS

    if service_by_name or service_by_unit:
        return SEARCH_KIND_SERVICE
    if has_sku or retail_by_unit:
        return SEARCH_KIND_RETAIL
    return SEARCH_KIND_UNCLEAR


def search_channel_for_kind(kind: str) -> str:
    return "web" if kind == SEARCH_KIND_SERVICE else "market"


def needs_search_api_first(spec_item) -> bool:
    """Услуга или товар, которого на Маркете скорее нет — Search API сразу, без Маркета."""
    return classify_search_kind(spec_item) in {SEARCH_KIND_SERVICE, SEARCH_KIND_UNCLEAR}


def manufacturer_is_legal_name(value) -> bool:
    text = _norm(value)
    if not text:
        return False
    if text.startswith("ип"):
        return True
    return any(marker in text for marker in LEGAL_NAME_MARKERS)


def _characteristic_blobs(spec_item) -> list[str]:
    raw = getattr(spec_item, "sku_characteristics_json", None)
    if not raw:
        return []
    try:
        rows = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(rows, list):
        return []
    blobs = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        blobs.append(f"{row.get('name') or ''} {row.get('value') or ''}".strip())
    return [blob for blob in blobs if blob]


def item_has_size(spec_item) -> bool:
    parts = [
        str(getattr(spec_item, "name", "") or ""),
        str(getattr(spec_item, "sku_model", "") or ""),
        *_characteristic_blobs(spec_item),
    ]
    blob = " ".join(parts).lower()
    return any(token in blob for token in SIZE_HINTS)


def color_is_flexible(spec_item) -> bool:
    """У сухих смесей цвет мешка серый/белый не отдельная модель."""
    name = _norm(getattr(spec_item, "name", None))
    return any(marker in name for marker in COMMODITY_COLOR_MARKERS)


def gost_from_item(spec_item) -> str:
    for blob in _characteristic_blobs(spec_item):
        lowered = blob.lower()
        if "гост" in lowered or "gost" in lowered:
            return " ".join(blob.split())
    return ""


def _is_model_code(text: str) -> bool:
    """Короткий артикул вроде АЛЮМ750, а не размер 800*450*1350мм."""
    compact = " ".join(text.split())
    if not compact or len(compact) > 24 or compact.count(" ") > 2:
        return False
    if not re.search(r"\d", compact):
        return False
    if re.fullmatch(r"\d{5,}", compact):
        return True
    return re.fullmatch(r"[\d\s*xх×.,/\-]+(мм|см|м)?", compact, flags=re.IGNORECASE) is None


def search_query_for_item(spec_item) -> str:
    name = str(getattr(spec_item, "name", "") or "").strip()
    model = str(getattr(spec_item, "sku_model", "") or "").strip()
    manufacturer = str(getattr(spec_item, "sku_manufacturer", "") or "").strip()
    ordered: list[str] = []
    if _is_model_code(model):
        ordered.append(model)
    ordered.extend((name, model, manufacturer))
    search_parts: list[str] = []
    for text in ordered:
        if not text:
            continue
        lowered = text.lower()
        if any(
            lowered in existing.lower() or existing.lower() in lowered
            for existing in search_parts
        ):
            continue
        search_parts.append(text)
    return " ".join(search_parts)


def web_search_query_for_item(spec_item, kind: str | None = None) -> str:
    """Запрос для Search API / yandex.ru: к услуге — тариф, к товару — купить."""
    base = search_query_for_item(spec_item)
    kind = kind or classify_search_kind(spec_item)
    gost = gost_from_item(spec_item)
    if gost and gost.lower() not in base.lower():
        base = f"{base} {gost}"
    if kind == SEARCH_KIND_SERVICE:
        suffix = "стоимость прайс"
    else:
        suffix = "купить"
    if suffix.split()[0] in base.lower():
        return base
    return f"{base} {suffix}".strip()


def search_start_url(search_text: str, kind: str) -> str:
    prefix = WEB_SEARCH_URL if kind == SEARCH_KIND_SERVICE else MARKET_SEARCH_URL
    return prefix + urllib.parse.quote(search_text)


def build_input_payload(
    pairs: list,
    search_stage: str | None = None,
    api_links_by_id: dict[int, list[dict]] | None = None,
) -> dict:
    batch_id = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
    items = []

    for auction, spec_item in pairs:
        tender_total = spec_item.total_price
        if tender_total is None and spec_item.price_per_unit is not None and spec_item.quantity is not None:
            tender_total = float(spec_item.price_per_unit) * float(spec_item.quantity)

        # В поиск кладём название, модель и производителя. Все характеристики туда не пишем:
        # запрос станет слишком узким и выдача будет пустой.
        search_text = search_query_for_item(spec_item)
        kind = classify_search_kind(spec_item)
        api_links = (api_links_by_id or {}).get(spec_item.id)
        api_first = search_stage == "api" or needs_search_api_first(spec_item)
        legal_name = manufacturer_is_legal_name(spec_item.sku_manufacturer)
        has_size = item_has_size(spec_item)
        flexible_color = color_is_flexible(spec_item)
        if api_first:
            channel = "web"
            start_kind = SEARCH_KIND_SERVICE
        else:
            channel = search_channel_for_kind(kind)
            start_kind = kind

        characteristics = []
        if spec_item.sku_characteristics_json:
            characteristics = json.loads(spec_item.sku_characteristics_json)

        item = {
            "specification_item_id": spec_item.id,
            "auction_number": auction.auction_number,
            "auction_name": auction.name,
            "tender_initial_price": (
                float(auction.initial_price) if auction.initial_price is not None else None
            ),
            "position_index": spec_item.position_index,
            "name": spec_item.name,
            "model": spec_item.sku_model,
            "manufacturer": spec_item.sku_manufacturer,
            "characteristics": characteristics,
            "quantity": float(spec_item.quantity) if spec_item.quantity is not None else None,
            "unit": spec_item.unit,
            "tender_price_per_unit": (
                float(spec_item.price_per_unit) if spec_item.price_per_unit is not None else None
            ),
            "tender_line_total": float(tender_total) if tender_total is not None else None,
            "item_kind": kind,
            "search_channel": channel,
            "search_plan": (
                "api_first" if api_first else ("market_then_api" if api_links else "market_only")
            ),
            "manufacturer_is_legal_name": legal_name,
            "has_size": has_size,
            "color_is_flexible": flexible_color,
            "yandex_search_url": search_start_url(search_text, start_kind),
        }
        if getattr(spec_item, "market_browser_attempts", 0):
            item["previous_attempt"] = {
                "browser_attempts": spec_item.market_browser_attempts,
                "fail_reason": getattr(spec_item, "market_fail_reason", None),
                "note": getattr(spec_item, "market_note", None),
            }
        if search_stage:
            item["search_stage"] = search_stage
        if api_links:
            item["yandex_api_links"] = api_links
        items.append(item)

    return {
        "batch_id": batch_id,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "items": items,
    }
