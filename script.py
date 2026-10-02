import math
import re
from datetime import datetime, timezone

from playwright.sync_api import Playwright, sync_playwright

from repository import save_purchases
from schemas import Purchase

FILTERED_PAGE = (
    "https://zakupki.mos.ru/purchase/list?page=1&perPage=50&sortField=PublishDate&sortDesc=true"
    "&filter=%7B%22typeIn%22%3A%7B%22values%22%3A%5B1%5D%7D%2C%22nameLike%22%3A%7B%22contains%22%3Atrue%7D"
    "%2C%22regionPaths%22%3A%7B%22values%22%3A%5B%22.1.504.%22%5D%7D%2C%22customerInnOrName%22%3A%7B%22contains%22%3Atrue%7D"
    "%2C%22numberLike%22%3A%7B%22contains%22%3Atrue%7D%2C%22externalNumberLike%22%3A%7B%22contains%22%3Atrue%7D"
    "%2C%22auctionSpecificFilter%22%3A%7B%22stateIdIn%22%3A%5B19000002%5D%2C%22initialDuration%22%3A%5B%5D"
    "%2C%22supplierTotalPoints%22%3A%7B%7D%7D%2C%22needSpecificFilter%22%3A%7B%22supplierTotalPoints%22%3A%7B%7D%7D"
    "%2C%22tenderSpecificFilter%22%3A%7B%7D%2C%22ptkrSpecificFilter%22%3A%7B%7D%7D"
    "&state=%7B%22currentTab%22%3A8%7D"
)

CARDS_PER_PAGE = 50
BASE_SITE_URL = "https://zakupki.mos.ru"


def set_page_in_url(url: str, page_number: int) -> str:
    return re.sub(r"page=\d+", f"page={page_number}", url)


def parse_found_count(raw_text: str) -> int:
    digits_only = re.sub(r"\D", "", raw_text)
    return int(digits_only) if digits_only else 0


def price_to_number(price_text: str) -> float | None:
    if not price_text:
        return None

    cleaned = price_text.replace("₽", "").replace("\xa0", " ")
    cleaned = cleaned.replace(" ", "").strip()
    cleaned = cleaned.replace(",", ".")

    if not cleaned:
        return None

    return float(cleaned)


def wait_for_list_loaded(page) -> int:
    page.locator("span", has_text="Найдено").locator("b").wait_for()
    page.locator('div[class*="PublicListContainer"]').wait_for()

    found_text = page.locator("span", has_text="Найдено").locator("b").inner_text()
    return parse_found_count(found_text)


def build_detail_url(relative_href: str | None) -> str:
    if not relative_href:
        return ""

    path = relative_href.split("?")[0]
    if path.startswith("http"):
        return path

    return BASE_SITE_URL + path


def scrape_card(card, source_page: int) -> Purchase:
    number = card.locator('a[class*="MainInfoNumberHeader"]').inner_text().strip()
    name = card.locator('a[class*="MainInfoNameHeader"]').inner_text().strip()
    price_text = card.locator('div[class*="PriceInfoNumber"]').inner_text().strip()
    customer = card.locator('a[class*="MainInfoCustomerHeader"]').inner_text().strip()
    status = card.locator("#state-indicator").inner_text().strip()
    purchase_type = card.locator('div[class*="MainInfoTypeHeader"] span').inner_text().strip()

    href = card.locator('a[href*="/auction/"]').first.get_attribute("href")
    detail_url = build_detail_url(href)

    additional = card.locator(
        'xpath=following-sibling::div[contains(@class, "AdditionalInfoContainer")][1]'
    )
    extra_spans = additional.locator('div[class*="AdditionalInfoHeader"] span').all_inner_texts()

    region = extra_spans[0].strip() if len(extra_spans) > 0 else ""
    law = extra_spans[1].strip() if len(extra_spans) > 1 else ""
    date_range = extra_spans[2].strip() if len(extra_spans) > 2 else ""
    integration = extra_spans[3].strip() if len(extra_spans) > 3 else ""

    return Purchase(
        auction_number=number,
        name=name,
        initial_price=price_to_number(price_text),
        customer=customer,
        status=status,
        purchase_type=purchase_type,
        region=region,
        law=law,
        date_range=date_range,
        integration=integration,
        detail_url=detail_url,
        source_page=source_page,
        scraped_at=datetime.now(timezone.utc),
    )


def scrape_current_page(page, source_page: int) -> list[Purchase]:
    list_container = page.locator('div[class*="PublicListContainer"]')
    cards = list_container.locator('div[class*="MainInfoContainer"]')

    card_count = cards.count()
    print(f"  Карточек на странице {source_page}: {card_count}")

    purchases = []
    for index in range(card_count):
        purchase = scrape_card(cards.nth(index), source_page)
        purchases.append(purchase)

    return purchases


def run(playwright: Playwright) -> list[Purchase]:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()

    all_purchases: list[Purchase] = []

    page.goto(set_page_in_url(FILTERED_PAGE, 1))
    total_found = wait_for_list_loaded(page)

    if total_found == 0:
        print("Закупок не найдено.")
        browser.close()
        return all_purchases

    total_pages = math.ceil(total_found / CARDS_PER_PAGE)
    print(f"Найдено закупок: {total_found}")
    print(f"Страниц для обхода: {total_pages}")

    for page_number in range(1, total_pages + 1):
        print(f"\nСтраница {page_number}/{total_pages}")

        if page_number > 1:
            page.goto(set_page_in_url(FILTERED_PAGE, page_number))
            wait_for_list_loaded(page)

        page_purchases = scrape_current_page(page, page_number)
        all_purchases.extend(page_purchases)

    browser.close()

    print(f"\nИтого собрано: {len(all_purchases)}")

    if all_purchases:
        saved_count = save_purchases(all_purchases)
        print(f"Сохранено в БД: {saved_count}")
        print("Пример первой записи:")
        print(all_purchases[0])

    return all_purchases


if __name__ == "__main__":
    with sync_playwright() as playwright:
        run(playwright)
