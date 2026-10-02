"""Общая сборка INPUT для export (Claude и Yandex)."""

import urllib.parse
from datetime import datetime, timezone


def build_input_payload(pairs: list) -> dict:
    batch_id = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
    items = []

    for auction, spec_item in pairs:
        tender_total = spec_item.total_price
        if tender_total is None and spec_item.price_per_unit is not None and spec_item.quantity is not None:
            tender_total = float(spec_item.price_per_unit) * float(spec_item.quantity)

        items.append(
            {
                "specification_item_id": spec_item.id,
                "auction_number": auction.auction_number,
                "auction_name": auction.name,
                "tender_initial_price": (
                    float(auction.initial_price) if auction.initial_price is not None else None
                ),
                "position_index": spec_item.position_index,
                "name": spec_item.name,
                "quantity": float(spec_item.quantity) if spec_item.quantity is not None else None,
                "unit": spec_item.unit,
                "tender_price_per_unit": (
                    float(spec_item.price_per_unit) if spec_item.price_per_unit is not None else None
                ),
                "tender_line_total": float(tender_total) if tender_total is not None else None,
                "yandex_search_url": (
                    "https://yandex.ru/search?products_mode=1&text="
                    + urllib.parse.quote(spec_item.name.strip())
                ),
            }
        )

    return {
        "batch_id": batch_id,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "items": items,
    }
