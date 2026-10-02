"""
Тест: 10 реальных названий из БД → поиск ya.ru «Товары» → печать карточек в консоль.

В БД ничего не пишем. Нужен Chrome с портом 9222 (ярлык debug).

Запуск:
    python ya_patchright_marketing_scan.py
"""

import urllib.parse

from patchright.sync_api import sync_playwright

from price_research_yandex_repository import get_next_yandex_pending_items

CDP_URL = "http://127.0.0.1:9222"
ITEM_LIMIT = 10
MAX_CARDS = 20
REGION_ID = 213
PAGE_LOAD_TIMEOUT_MS = 60_000
AFTER_LOAD_WAIT_MS = 3_000
CAPTCHA_WAIT_MS = 120_000
PAUSE_BETWEEN_SEARCHES_MS = 3_000

# Карточки вкладки «Товары» на ya.ru, не Маркет.
CARD_SELECTOR = ".ProductTile-Item"


def build_search_url(query: str) -> str:
    params = {
        "text": query.strip(),
        "products_mode": "1",
        "lr": str(REGION_ID),
    }
    encoded = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    return f"https://ya.ru/search?{encoded}"


def page_has_captcha(page) -> bool:
    title = page.title()
    if "не робот" in title.lower():
        return True
    if page.get_by_text("Вы не робот").count() > 0:
        return True
    if page.get_by_text("Подтвердите, что вы не робот").count() > 0:
        return True
    return False


def wait_after_search(page) -> bool:
    """
    Ждём карточки. Если капча — ждём, пока вы пройдёте её руками.
    Возвращает True, если капча была на этой позиции.
    """
    page.wait_for_timeout(AFTER_LOAD_WAIT_MS)
    saw_captcha = page_has_captcha(page)
    cards = page.locator(CARD_SELECTOR)

    if saw_captcha:
        print("  >>> КАПЧА. Пройдите её в окне Chrome, скрипт подождёт...")
        cards.first.wait_for(state="visible", timeout=CAPTCHA_WAIT_MS)
        page.wait_for_timeout(1500)

    return saw_captcha


def collect_cards(page) -> list[dict]:
    """
    ЗАМЕТКА ДЛЯ ОБУЧЕНИЯ:
    inner_text() по одной карточке — это отдельный вопрос к Chrome.
    20 карточек × 3 поля ≈ 60 поездок туда-сюда, поэтому вывод казался «поштучным».
    evaluate() один раз выполняет JS в странице и возвращает сразу весь список.
    """
    return page.evaluate(
        """(maxCards) => {
            const nodes = [...document.querySelectorAll('.ProductTile-Item')];
            const take = nodes.slice(0, maxCards);
            const textOf = (root, selector) => {
                const el = root.querySelector(selector);
                if (!el) {
                    return '';
                }
                return el.innerText.replace(/\\s+/g, ' ').trim();
            };
            return {
                totalOnPage: nodes.length,
                cards: take.map((card) => ({
                    name: textOf(card, '.EProductSnippet2-Title'),
                    price: textOf(card, '.EPriceGroup-Price .EPrice-Value'),
                    shop: textOf(card, '.EShopName .Line-AddonContent'),
                })),
            };
        }""",
        MAX_CARDS,
    )


def print_cards(page) -> None:
    result = collect_cards(page)
    cards = result["cards"]
    print(f"  карточек на странице: {result['totalOnPage']}, берём: {len(cards)}")

    lines = []
    for index, card in enumerate(cards, start=1):
        price_text = card["price"] or "—"
        lines.append(f"  [{index}] {price_text} руб — {card['name']} | {card['shop']}")
    print("\n".join(lines))


pairs = get_next_yandex_pending_items(limit=ITEM_LIMIT)
if not pairs:
    print("Нет pending-позиций Yandex в БД (нужен detail_status=done).")
    raise SystemExit(1)

print(f"К сканированию: {len(pairs)} позиций\n")

with sync_playwright() as playwright:
    browser = playwright.chromium.connect_over_cdp(CDP_URL)
    context = browser.contexts[0]
    page = context.new_page()

    captcha_count = 0

    for number, (auction, spec_item) in enumerate(pairs, start=1):
        query = spec_item.name.strip()
        search_url = build_search_url(query)

        print("=" * 60)
        print(f"[{number}/{len(pairs)}] id={spec_item.id}  тендер={auction.auction_number}")
        print(f"  запрос: {query}")

        page.goto(search_url, timeout=PAGE_LOAD_TIMEOUT_MS, wait_until="domcontentloaded")
        saw_captcha = wait_after_search(page)
        if saw_captcha:
            captcha_count += 1

        print(f"  URL: {page.url}")
        print(f"  Title: {page.title()}")
        print_cards(page)

        if number < len(pairs):
            page.wait_for_timeout(PAUSE_BETWEEN_SEARCHES_MS)

    print("=" * 60)
    print("ИТОГО")
    print(f"  запросов: {len(pairs)}")
    print(f"  капч: {captcha_count}")

    # browser.close() не вызываем — это закрыло бы весь Chrome.
