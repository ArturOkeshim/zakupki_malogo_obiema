"""
Карточки СТЕ на zakupki.mos.ru: модель, производитель, характеристики.

Запуск отдельно:
    python sku_card.py 10311004 10311003
"""

import random
import time

from playwright.sync_api import Page, sync_playwright

from detail import (
    RESTART_WAIT_MAX_SEC,
    RESTART_WAIT_MIN_SEC,
    create_browser,
    pause_between_purchases,
)
from detail_repository import get_specification_items_by_auctions, save_sku_card_details


def read_bold_label(page: Page, label: str) -> str | None:
    """
    На карточке это строка вида <span>Модель: <b>1076337</b></span>.
    Берём текст из <b>, а не всю строку целиком.
    """
    spans = page.locator("span", has_text=label)
    for index in range(spans.count()):
        bold = spans.nth(index).locator("b")
        if bold.count() == 0:
            continue
        text = bold.first.inner_text().strip()
        if text:
            return text
    return None


def read_characteristics(page: Page) -> list[dict]:
    """
    Строки «Все характеристики» уже лежат в HTML, радиокнопку не нажимаем.
    ЗАМЕТКА ДЛЯ ОБУЧЕНИЯ: у сайта класс выглядит как SkuViewStyles__CharacteristicRow-хеш.
    Хеш после дефиса меняется при каждой сборке сайта, поэтому ищем по куску CharacteristicRow, а не по хешу.
    """
    rows = page.locator('[class*="CharacteristicRow"]')
    characteristics = []
    for index in range(rows.count()):
        row = rows.nth(index)
        name_node = row.locator('[class*="CharName"]')
        value_node = row.locator('[class*="CharaValue"]')
        if name_node.count() == 0 or value_node.count() == 0:
            continue
        name = name_node.first.inner_text().strip()
        value = value_node.first.inner_text().strip()
        if not name:
            continue
        characteristics.append({"name": name, "value": value})
    return characteristics


def wait_for_sku_card(page: Page) -> None:
    # Любой из этих кусков значит, что открылась карточка, а не пустая или ошибочная страница.
    page.locator(
        'span:has-text("Модель:"), span:has-text("Производитель:"), [class*="CharacteristicRow"]'
    ).first.wait_for(timeout=60_000)


def scrape_sku_card(page: Page, sku_url: str) -> dict:
    page.goto(sku_url, timeout=60_000)
    wait_for_sku_card(page)
    return {
        "model": read_bold_label(page, "Модель:"),
        "manufacturer": read_bold_label(page, "Производитель:"),
        "characteristics": read_characteristics(page),
    }


def scrape_and_save(page: Page, item) -> bool:
    try:
        card = scrape_sku_card(page, item.sku_url)
    except Exception as error:
        print(f"  не открылась: {error}")
        return False

    save_sku_card_details(
        item.id,
        card["model"],
        card["manufacturer"],
        card["characteristics"],
    )
    print(
        f"  OK — модель: {card['model'] or '—'}, "
        f"производитель: {card['manufacturer'] or '—'}, "
        f"характеристик: {len(card['characteristics'])}"
    )
    return True


def already_scraped(item) -> bool:
    if item.sku_scraped_at is not None:
        return True
    if item.sku_model and item.sku_model.strip():
        return True
    return False


def run_for_numbers(auction_numbers: list[str]) -> None:
    items = get_specification_items_by_auctions(auction_numbers)
    if not items:
        print("Нет позиций спецификации для этих закупок.")
        return

    with sync_playwright() as playwright:
        browser, page = create_browser(playwright)
        try:
            for index, item in enumerate(items, start=1):
                print(f"\n[{index}] {item.auction_number} позиция {item.position_index}")
                print(f"  {item.name}")

                if not item.sku_url:
                    print("  нет ссылки на карточку, оставляем как есть")
                    continue

                if already_scraped(item):
                    print("  карточка уже снята, не открываю")
                    continue

                print(f"  URL: {item.sku_url}")
                if scrape_and_save(page, item):
                    pause_between_purchases()
                    continue

                cooldown = random.uniform(RESTART_WAIT_MIN_SEC, RESTART_WAIT_MAX_SEC)
                print(f"  пауза {cooldown:.1f} сек и вторая попытка")
                time.sleep(cooldown)
                browser.close()
                browser, page = create_browser(playwright)

                if scrape_and_save(page, item):
                    pause_between_purchases()
                    continue

                print("  вторая попытка не удалась, пропускаю позицию")
                pause_between_purchases()
        finally:
            browser.close()


def main() -> None:
    import sys

    auction_numbers = sys.argv[1:]
    if not auction_numbers:
        raise SystemExit("Укажите номера закупок: python sku_card.py 10311004")
    run_for_numbers(auction_numbers)


if __name__ == "__main__":
    main()
