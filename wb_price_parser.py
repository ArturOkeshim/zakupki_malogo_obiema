"""
Парсер цен Wildberries по позициям из specification_items.

Берёт name из спецификации, ищет на WB, сохраняет карточки в таблицу wb_offers.
match_status = pending — для следующего этапа (нейросеть проверит, подходит ли товар).

Запуск:
    python wb_price_parser.py
    python wb_price_parser.py --query "счётчик воды Decast"
"""

import argparse
import time
import urllib.parse

from playwright.sync_api import Locator, Page, sync_playwright

from parsing import price_to_number
from wb_price_parser_repository import (
    get_auctions_batch,
    get_specification_items,
    save_analysis_status,
    save_wb_offers,
)
from wb_price_parser_schemas import WbProductCard

QUEUE_BATCH_SIZE = 1
PAUSE_BETWEEN_SEARCHES_SEC = 5
PAGE_LOAD_TIMEOUT_MS = 60_000
PRODUCT_LIST_TIMEOUT_MS = 30_000
MAX_CARDS_TO_PARSE = 5
HEADLESS = False

WB_SEARCH_URL = "https://www.wildberries.ru/catalog/0/search.aspx"


def build_wb_search_url(product_name: str) -> str:
    cleaned_name = product_name.strip()
    query = urllib.parse.quote(cleaned_name)
    return f"{WB_SEARCH_URL}?search={query}"


def wait_for_product_list(page: Page) -> Locator:
    product_list = page.locator("div.product-card-list")
    product_list.wait_for(state="visible", timeout=PRODUCT_LIST_TIMEOUT_MS)
    page.wait_for_timeout(1_500)
    return product_list


def scrape_one_card(link: Locator) -> WbProductCard | None:
    product_url = link.get_attribute("href")
    if not product_url:
        return None

    if product_url.startswith("/"):
        product_url = "https://www.wildberries.ru" + product_url

    name = link.get_attribute("aria-label") or ""
    if not name:
        name = link.inner_text().strip()

    card = link.locator('xpath=ancestor::article[1]')
    if card.count() == 0:
        card = link.locator('xpath=ancestor::div[contains(@class, "product-card")][1]')

    price_text = ""
    old_price_text = ""
    if card.count() > 0:
        price_block = card.locator(".product-card__price ins").first
        if price_block.count() > 0:
            price_text = price_block.inner_text()

        old_price_block = card.locator(".product-card__price del").first
        if old_price_block.count() > 0:
            old_price_text = old_price_block.inner_text()

    if not name:
        return None

    return WbProductCard(
        name=name,
        price=price_to_number(price_text),
        old_price=price_to_number(old_price_text),
        url=product_url,
    )


def scrape_wb_search_results(page: Page, max_cards: int = MAX_CARDS_TO_PARSE) -> list[WbProductCard]:
    product_list = wait_for_product_list(page)
    links = product_list.locator('a[data-testid="product-card-link"]')
    links_count = links.count()

    if links_count == 0:
        raise ValueError("Список загрузился, но карточки не найдены")

    cards: list[WbProductCard] = []
    for index in range(min(links_count, max_cards)):
        card = scrape_one_card(links.nth(index))
        if card is not None:
            cards.append(card)

    if not cards:
        raise ValueError("Не удалось разобрать ни одной карточки")

    return cards


def search_on_wildberries(page: Page, product_name: str) -> list[WbProductCard]:
    search_url = build_wb_search_url(product_name)

    print(f"  URL: {search_url}")
    page.goto(search_url, timeout=PAGE_LOAD_TIMEOUT_MS)
    page.wait_for_load_state("domcontentloaded")

    cards = scrape_wb_search_results(page)

    print(f"  найдено карточек: {len(cards)}")
    for index, card in enumerate(cards, start=1):
        print(f"    [{index}] {card.price} ₽ — {card.name[:80]}")
        print(f"         {card.url}")

    return cards


def process_auction(page: Page, auction_number: str) -> bool:
    items = get_specification_items(auction_number)
    if not items:
        save_analysis_status(auction_number, "failed", "Нет позиций в specification_items")
        print("  ОШИБКА: нет позиций в спецификации")
        return False

    print(f"  позиций: {len(items)}")

    try:
        for index, item in enumerate(items, start=1):
            print(f"\n  [{index}/{len(items)}] {item.name[:100]}")
            print(f"  цена в аукционе: {item.price_per_unit} ₽")

            cards = search_on_wildberries(page, item.name)
            saved_count = save_wb_offers(
                specification_item_id=item.id,
                auction_number=auction_number,
                search_query=item.name,
                cards=cards,
            )
            print(f"  сохранено в wb_offers: {saved_count}")

            if index < len(items):
                time.sleep(PAUSE_BETWEEN_SEARCHES_SEC)

        save_analysis_status(auction_number, "done")
        return True
    except Exception as error:
        save_analysis_status(auction_number, "failed", str(error))
        print(f"  ОШИБКА: {error}")
        return False


def run_test_query(page: Page, query: str) -> None:
    print(f"Тестовый запрос WB (без сохранения в БД): {query}")
    search_on_wildberries(page, query)
    print("Готово.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Парсер цен Wildberries")
    parser.add_argument(
        "--query",
        help="Тест без БД: искать этот текст на Wildberries",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Повторить аукционы с analysis_status=failed",
    )
    args = parser.parse_args()

    success_count = 0
    failed_count = 0
    processed_count = 0

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=HEADLESS)
        page = browser.new_page(locale="ru-RU")

        if args.query:
            run_test_query(page, args.query)
            browser.close()
            return

        while True:
            batch = get_auctions_batch(QUEUE_BATCH_SIZE, include_failed=args.retry_failed)
            if not batch:
                break

            print(f"\n--- Очередь парсинга WB: {len(batch)} аукцион(ов) ---")

            for auction in batch:
                processed_count += 1
                print(f"\n[{processed_count}] {auction.auction_number}")
                print(f"  {auction.name[:120]}")

                if process_auction(page, auction.auction_number):
                    success_count += 1
                else:
                    failed_count += 1

        browser.close()

    if processed_count == 0:
        print("Нет аукционов для парсинга WB.")
        print("(нужны записи с detail_status=done и analysis_status=pending)")
        if not args.retry_failed:
            print("Подсказка: если прошлый запуск упал — попробуйте python wb_price_parser.py --retry-failed")
    else:
        print(f"\nИтого: успешно {success_count}, ошибок {failed_count}, всего {processed_count}")


if __name__ == "__main__":
    main()
