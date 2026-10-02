"""
Учебный скрипт: поиск товара на Wildberries и сбор первых карточек.

Запуск:
    python wb_search_test.py
    python wb_search_test.py "наушники беспроводные"
"""

import argparse
import urllib.parse

from patchright.sync_api import sync_playwright

# --- Настройки ---
SEARCH_QUERY = "кроссовки женские"
MAX_CARDS = 5
PAGE_LOAD_TIMEOUT_MS = 60_000
PRODUCT_LIST_TIMEOUT_MS = 45_000

WB_SEARCH_URL = "https://www.wildberries.ru/catalog/0/search.aspx"


def build_search_url(query: str) -> str:
    cleaned_query = query.strip()
    encoded_query = urllib.parse.quote(cleaned_query)
    return f"{WB_SEARCH_URL}?search={encoded_query}"


def price_text_to_number(price_text: str) -> float | None:
    if not price_text:
        return None

    cleaned = price_text.replace("₽", "").replace("\xa0", " ")
    cleaned = cleaned.replace(" ", "").strip()
    cleaned = cleaned.replace(",", ".")

    if not cleaned:
        return None

    return float(cleaned)


def wait_for_product_list(page):
    """
    Ждём, пока JS подгрузит список карточек.
    ЗАМЕТКА ДЛЯ ОБУЧЕНИЯ:
    WB не отдаёт карточки в HTML сразу — их рисует JavaScript после API-запроса.
    Поэтому requests/BeautifulSoup тут не сработают без браузера.
    """
    product_list = page.locator("div.product-card-list")
    product_list.wait_for(state="visible", timeout=PRODUCT_LIST_TIMEOUT_MS)

    # Небольшая пауза: иногда цены дорисовываются чуть позже названий.
    page.wait_for_timeout(1500)

    return product_list


def scrape_one_card(link) -> dict | None:
    product_url = link.get_attribute("href")
    if not product_url:
        return None

    if product_url.startswith("/"):
        product_url = "https://www.wildberries.ru" + product_url

    name = link.get_attribute("aria-label") or link.inner_text().strip()

    card = link.locator('xpath=ancestor::article[1]')
    if card.count() == 0:
        card = link.locator('xpath=ancestor::div[contains(@class, "product-card")][1]')

    price_text = ""
    if card.count() > 0:
        price_block = card.locator(".product-card__price ins").first
        if price_block.count() > 0:
            price_text = price_block.inner_text()

    if not name:
        return None

    return {
        "name": name,
        "price": price_text_to_number(price_text),
        "price_text": price_text,
        "url": product_url,
    }


def scrape_first_cards(page, max_cards: int) -> list[dict]:
    product_list = wait_for_product_list(page)
    links = product_list.locator('a[data-testid="product-card-link"]')
    links_count = links.count()

    if links_count == 0:
        raise ValueError("Список загрузился, но ссылки на карточки не найдены")

    cards = []
    for index in range(min(links_count, max_cards)):
        card = scrape_one_card(links.nth(index))
        if card is not None:
            cards.append(card)

    if not cards:
        raise ValueError("Карточки есть, но не удалось прочитать название/цену")

    return cards


def main() -> None:
    parser = argparse.ArgumentParser(description="Учебный парсер Wildberries")
    parser.add_argument("query", nargs="?", default=SEARCH_QUERY, help="Поисковый запрос")
    parser.add_argument("--max-cards", type=int, default=MAX_CARDS, help="Сколько карточек вывести")
    args = parser.parse_args()

    search_url = build_search_url(args.query)
    print(f"Запрос: {args.query}")
    print(f"URL: {search_url}")

    with sync_playwright() as playwright:
        # ЗАМЕТКА ДЛЯ ОБУЧЕНИЯ:
        # patchright — форк Playwright, который меньше «светится» как бот.
        # headless=False — видимое окно; WB чаще блокирует headless=True.
        browser = playwright.chromium.launch(headless=False)
        page = browser.new_page(locale="ru-RU")

        page.goto(search_url, timeout=PAGE_LOAD_TIMEOUT_MS)
        page.wait_for_load_state("domcontentloaded")

        cards = scrape_first_cards(page, args.max_cards)

        print(f"\nНайдено карточек: {len(cards)}")
        for index, card in enumerate(cards, start=1):
            price_display = card["price_text"] or "—"
            print(f"[{index}] {price_display} — {card['name'][:80]}")
            print(f"     {card['url']}")

        browser.close()


if __name__ == "__main__":
    main()
