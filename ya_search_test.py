"""
Учебный скрипт: поиск товара в Яндексе (вкладка «Товары»)
и сбор карточек: промо-галерея + предложения магазинов.

Запуск:
    python ya_search_test.py
    python ya_search_test.py "Счётчик холодной воды Decast ОСВХ-25"
"""

import argparse
import urllib.parse

from patchright.sync_api import sync_playwright

# --- Настройки ---
SEARCH_QUERY = "Счётчик холодной воды Decast ОСВХ-25 «НЕПТУН» цена"
MAX_CARDS = 8
PAGE_LOAD_TIMEOUT_MS = 60_000
CARDS_TIMEOUT_MS = 45_000
CAPTCHA_WAIT_MS = 120_000
# lr=213 — регион Москва, как в вашей ссылке.
REGION_ID = 213

YA_SEARCH_URL = "https://ya.ru/search"


def build_search_url(query: str) -> str:
    cleaned_query = query.strip()
    params = {
        "text": cleaned_query,
        "products_mode": "1",
        "from": "tabbar",
        "serp-reload-from": "tab",
        "lr": str(REGION_ID),
    }
    encoded = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    return f"{YA_SEARCH_URL}?{encoded}"


def price_text_to_number(price_text: str) -> float | None:
    if not price_text:
        return None

    cleaned = price_text.replace("₽", "").replace("\xa0", " ")
    cleaned = cleaned.replace(" ", "").strip()
    cleaned = cleaned.replace(",", ".")

    if not cleaned:
        return None

    return float(cleaned)


def first_text(root, selector: str) -> str:
    block = root.locator(selector)
    if block.count() == 0:
        return ""
    raw_text = block.first.inner_text().strip()
    # split() убирает переносы и невидимые пробелы внутри текста карточки.
    return " ".join(raw_text.split())


def first_href(root) -> str:
    overlay = root.locator("a.EProductSnippet2-Overlay")
    if overlay.count() > 0:
        href = overlay.first.get_attribute("href") or ""
        if href:
            return href

    any_link = root.locator("a[href]")
    if any_link.count() == 0:
        return ""
    return any_link.first.get_attribute("href") or ""


def page_has_captcha(page) -> bool:
    captcha_markers = [
        "Вы не робот",
        "Подтвердите, что вы не робот",
    ]
    for marker in captcha_markers:
        if page.get_by_text(marker).count() > 0:
            return True
    return False


def wait_for_cards(page) -> None:
    """
    Ждём либо карточки, либо капчу.
    ЗАМЕТКА ДЛЯ ОБУЧЕНИЯ:
    Яндекс часто показывает SmartCaptcha. headless=False даёт шанс пройти её руками.
    """
    cards = page.locator(".AdvProductGalleryCard, .ProductTile-Item")

    if page_has_captcha(page):
        print("Яндекс показал капчу. Пройдите её в окне браузера...")
        cards.first.wait_for(state="visible", timeout=CAPTCHA_WAIT_MS)
    else:
        cards.first.wait_for(state="visible", timeout=CARDS_TIMEOUT_MS)

    # Цены и названия иногда дорисовываются чуть позже каркаса карточки.
    page.wait_for_timeout(1500)


def scrape_one_card(card, source: str) -> dict | None:
    name = first_text(card, ".EProductSnippet2-Title")
    price_text = first_text(card, ".EPriceGroup-Price .EPrice-Value")
    old_price_text = first_text(card, ".EPriceGroup-PriceOld .EPrice-Value")
    shop = first_text(card, ".EProductSnippet2-ShopInfoTitle")
    if not shop:
        shop = first_text(card, ".EProductSnippet2-ShopInfo")
    discount = first_text(card, ".EPriceGroup-LabelDiscount")
    url = first_href(card)

    if not name:
        return None

    return {
        "source": source,
        "name": name,
        "price": price_text_to_number(price_text),
        "price_text": price_text,
        "old_price_text": old_price_text,
        "shop": shop,
        "discount": discount,
        "url": url,
    }


def scrape_cards_from_locator(locator, source: str, max_cards: int) -> list[dict]:
    cards = []
    cards_count = locator.count()

    for index in range(min(cards_count, max_cards)):
        card = scrape_one_card(locator.nth(index), source)
        if card is not None:
            cards.append(card)

    return cards


def scrape_yandex_cards(page, max_cards: int) -> list[dict]:
    wait_for_cards(page)

    # Сначала рекламная карусель («карточки промо»), потом сетка магазинов.
    promo_locator = page.locator(".AdvProductGalleryCard")
    shop_locator = page.locator(".ProductTile-Item")

    promo_cards = scrape_cards_from_locator(promo_locator, "промо", max_cards)
    remaining = max(max_cards - len(promo_cards), 0)
    shop_cards = scrape_cards_from_locator(shop_locator, "магазин", remaining)

    cards = promo_cards + shop_cards
    if not cards:
        raise ValueError("Карточки видны, но не удалось прочитать название")

    return cards


def print_cards(cards: list[dict]) -> None:
    print(f"\nНайдено карточек: {len(cards)}")
    for index, card in enumerate(cards, start=1):
        price_display = card["price_text"] or "—"
        if card["old_price_text"]:
            price_display = f"{price_display} ₽ (было {card['old_price_text']} ₽)"
        elif card["price_text"]:
            price_display = f"{price_display} ₽"

        title = card["name"][:80]
        print(f"[{index}] [{card['source']}] {price_display} — {title}")

        extra_parts = []
        if card["shop"]:
            extra_parts.append(f"магазин: {card['shop']}")
        if card["discount"]:
            extra_parts.append(card["discount"])
        if extra_parts:
            print(f"     {' | '.join(extra_parts)}")
        if card["url"]:
            print(f"     {card['url']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Учебный парсер Яндекса (вкладка Товары)")
    parser.add_argument("query", nargs="?", default=SEARCH_QUERY, help="Поисковый запрос")
    parser.add_argument("--max-cards", type=int, default=MAX_CARDS, help="Сколько карточек вывести")
    args = parser.parse_args()

    search_url = build_search_url(args.query)
    print(f"Запрос: {args.query}")
    print(f"URL: {search_url}")

    with sync_playwright() as playwright:
        # ЗАМЕТКА ДЛЯ ОБУЧЕНИЯ:
        # Не импортируйте analyze.py / playwright сюда: конфликт с patchright.
        browser = playwright.chromium.launch(headless=False)
        page = browser.new_page(locale="ru-RU")

        page.goto(search_url, timeout=PAGE_LOAD_TIMEOUT_MS, wait_until="domcontentloaded")
        cards = scrape_yandex_cards(page, args.max_cards)
        print_cards(cards)

        browser.close()


if __name__ == "__main__":
    main()
