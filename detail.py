import random
import re
import time

from playwright.sync_api import Page, sync_playwright

from detail_repository import (
    get_active_purchases_for_detail,
    get_purchases_by_numbers,
    save_auction_detail,
)
from detail_schemas import AuctionDetail, DeliveryStage, DeliveryStageRow, SpecificationItem
from parsing import parse_deadline_from_date_range, parse_quantity, price_to_number

BASE_SITE_URL = "https://zakupki.mos.ru"

# Сколько закупок брать из БД за один запрос очереди.
# 1 = перечитывать перед каждой закупкой, 5 = каждые 5 закупок (рекомендуется при параллельном script.py)
QUEUE_BATCH_SIZE = 5
PAUSE_MIN_SEC = 2
PAUSE_MAX_SEC = 6
RESTART_WAIT_MIN_SEC = 8
RESTART_WAIT_MAX_SEC = 15


def create_browser(playwright):
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(locale="ru-RU")
    return browser, page


def pause_between_purchases() -> None:
    # ЗАМЕТКА ДЛЯ ОБУЧЕНИЯ:
    # random.uniform(a, b) даёт случайное дробное число от a до b.
    # Одинаковые паузы (ровно 3 секунды) легче распознать как бота.
    seconds = random.uniform(PAUSE_MIN_SEC, PAUSE_MAX_SEC)
    print(f"  пауза {seconds:.1f} сек")
    time.sleep(seconds)


def build_full_url(relative_url: str | None) -> str | None:
    if not relative_url:
        return None
    if relative_url.startswith("http"):
        return relative_url
    return BASE_SITE_URL + relative_url


def get_labeled_value(card, label_text: str) -> str:
    labeled_blocks = card.locator('div[class*="LabeledValue"]')
    block_count = labeled_blocks.count()

    for index in range(block_count):
        block = labeled_blocks.nth(index)
        label = block.locator("label").inner_text().strip()
        if label == label_text:
            return block.locator("div").last.inner_text().strip()

    return ""


def get_card_locator_from_sku_link(sku_link) -> object:
    """Поднимаемся от ссылки на товар к контейнеру карточки."""
    card = sku_link.locator('xpath=ancestor::div[contains(@class, "CardContainer")][1]')
    if card.count() > 0:
        return card

    return sku_link.locator('xpath=ancestor::div[contains(@class, "grid")][1]')


def get_card_locator_from_labeled_block(labeled_block) -> object:
    """
    Поднимаемся от поля «Количество» / «Цена за ед.» к карточке позиции.
    У услуг часто нет CardContainer — берём колонку или ближайший ui grid.
    """
    card = labeled_block.locator('xpath=ancestor::div[contains(@class, "CardContainer")][1]')
    if card.count() > 0:
        return card

    card = labeled_block.locator(
        'xpath=ancestor::div[contains(@class, "PaddedGridColumn")][1]/..'
    )
    if card.count() > 0:
        return card

    return labeled_block.locator('xpath=ancestor::div[contains(@class, "grid")][1]')


def get_unique_sku_links(container):
    """
    Одна позиция = одна уникальная ссылка /sku/.
    На странице часто две одинаковые ссылки (mobile + desktop) — оставляем одну.
    """
    links = container.locator('a[class*="CardHeader"][href*="/sku/"]')
    seen_hrefs: set[str] = set()
    unique_links = []

    for index in range(links.count()):
        link = links.nth(index)
        href = link.get_attribute("href")
        if not href or href in seen_hrefs:
            continue
        seen_hrefs.add(href)
        unique_links.append(link)

    return unique_links


def parse_item_quantity_and_unit(card) -> tuple[str, float | None, str | None]:
    """
    Количество может отсутствовать в основной карточке.
    Тогда берём только единицу измерения из «Ед. измерения».
    """
    quantity_text = get_labeled_value(card, "Количество")
    unit_text = get_labeled_value(card, "Ед. измерения")

    if quantity_text and quantity_text != "-":
        quantity, unit = parse_quantity(quantity_text)
        return quantity_text, quantity, unit

    if unit_text:
        return None, None, unit_text

    return None, None, None


def scrape_specification_card(card, position_index: int) -> SpecificationItem | None:
    sku_link = card.locator('a[class*="CardHeader"][href*="/sku/"]')
    if sku_link.count() > 0:
        name = sku_link.first.inner_text().strip()
        sku_url = build_full_url(sku_link.first.get_attribute("href"))
    else:
        # Название без SKU: a или div в строке названия (не блок «Общая стоимость»)
        name_header = card.locator('div[class*="MobileHiddenRow"] a[class*="CardHeader"]')
        if name_header.count() == 0:
            name_header = card.locator('div[class*="MobileHiddenRow"] div[class*="CardHeader"]')
        if name_header.count() == 0:
            name_header = card.locator('div[class*="MobileImageHeaderContainer"] a[class*="CardHeader"]')
        if name_header.count() == 0:
            name_header = card.locator('div[class*="MobileImageHeaderContainer"] div[class*="CardHeader"]')
        if name_header.count() == 0:
            name_header = card.locator('div[class*="CardHeader"]')
        if name_header.count() == 0:
            return None
        name = name_header.first.inner_text().strip()
        sku_url = None

    if not name:
        return None

    image = card.locator("img").first
    image_url = image.get_attribute("src") if image.count() > 0 else None

    quantity_text, quantity, unit = parse_item_quantity_and_unit(card)
    price_per_unit = price_to_number(get_labeled_value(card, "Цена за ед."))
    total_price = price_to_number(get_labeled_value(card, "Общая стоимость"))

    return SpecificationItem(
        position_index=position_index,
        name=name,
        sku_url=sku_url,
        image_url=image_url,
        quantity_text=quantity_text,
        quantity=quantity,
        unit=unit,
        price_per_unit=price_per_unit,
        total_price=total_price,
    )


def scrape_specification_by_field_label(container, label_text: str) -> list[SpecificationItem]:
    """Собирает позиции по блоку LabeledValue (например «Количество» или «Цена за ед.»)."""
    field_blocks = container.locator(
        f'div[class*="LabeledValue"]:has(label:text-is("{label_text}"))'
    )
    specification_items: list[SpecificationItem] = []
    seen_names: set[str] = set()

    for index in range(field_blocks.count()):
        field_block = field_blocks.nth(index)
        card = get_card_locator_from_labeled_block(field_block)
        item = scrape_specification_card(card, len(specification_items) + 1)
        if item is None:
            continue
        if item.name in seen_names:
            continue
        seen_names.add(item.name)
        specification_items.append(item)

    return specification_items


def scrape_delivery_stages(page: Page) -> list[DeliveryStage]:
    stages = []
    stage_blocks = page.locator('div[class*="DeliveryStageContainer"]')
    stage_count = stage_blocks.count()

    for stage_index in range(stage_count):
        stage_block = stage_blocks.nth(stage_index)

        stage_number_text = stage_block.locator("div").first.inner_text().strip()
        stage_number_match = re.search(r"\d+", stage_number_text)
        stage_number = int(stage_number_match.group()) if stage_number_match else stage_index + 1

        header_price_block = stage_block.locator('div[class*="DeliveryStageHeader"] div').last
        stage_price = price_to_number(header_price_block.inner_text()) if header_price_block.count() else None

        address_divs = stage_block.locator('div[class*="DeliveryStageAddress"] > div')
        address = address_divs.nth(0).inner_text().strip() if address_divs.count() > 0 else None
        date_range = address_divs.nth(1).inner_text().strip() if address_divs.count() > 1 else None

        rows = []
        table_rows = stage_block.locator("table tbody tr")
        for row_index in range(table_rows.count()):
            row = table_rows.nth(row_index)
            cells = row.locator("td")
            if cells.count() < 5:
                continue

            rows.append(
                DeliveryStageRow(
                    customer_name=cells.nth(0).inner_text().strip(),
                    name=cells.nth(1).inner_text().strip(),
                    price_per_unit=price_to_number(cells.nth(2).inner_text()),
                    quantity=price_to_number(cells.nth(3).inner_text()),
                    total_price=price_to_number(cells.nth(4).inner_text()),
                )
            )

        stages.append(
            DeliveryStage(
                stage_number=stage_number,
                stage_price=stage_price,
                address=address,
                date_range=date_range,
                rows=rows,
            )
        )

    return stages


def wait_for_detail_loaded(page: Page) -> None:
    container = page.locator('div[class*="AuctionViewSpecificationsContainer"]')
    container.wait_for(timeout=60_000)

    # ЗАМЕТКА ДЛЯ ОБУЧЕНИЯ:
    # Не все карточки имеют «Количество» в основном блоке.
    # Ждём любой признак, что спецификация загрузилась.
    ready_selectors = [
        'a[class*="CardHeader"][href*="/sku/"]',
        'div[class*="LabeledValue"]:has(label:text-is("Количество"))',
        'div[class*="LabeledValue"]:has(label:text-is("Цена за ед."))',
    ]

    for selector in ready_selectors:
        block = container.locator(selector).first
        try:
            block.wait_for(state="visible", timeout=10_000)
            return
        except Exception:
            continue

    raise ValueError("Спецификация не загрузилась: нет ни SKU, ни цены, ни количества")


def scrape_auction_detail(page: Page, detail_url: str) -> AuctionDetail:
    page.goto(detail_url, timeout=60_000)
    wait_for_detail_loaded(page)

    container = page.locator('div[class*="AuctionViewSpecificationsContainer"]')
    sku_links = get_unique_sku_links(container)

    specification_items = []

    if sku_links:
        # Основной путь: одна уникальная ссылка /sku/ = одна позиция.
        for index, sku_link in enumerate(sku_links):
            card = get_card_locator_from_sku_link(sku_link)
            item = scrape_specification_card(card, index + 1)
            if item is None:
                continue
            specification_items.append(item)
    else:
        # Запасной путь без SKU: сначала «Количество», иначе «Цена за ед.» (услуги, часы и т.п.).
        for label in ("Количество", "Цена за ед."):
            specification_items = scrape_specification_by_field_label(container, label)
            if specification_items:
                break

    if not specification_items:
        raise ValueError("Не найдено позиций в спецификации на странице")

    delivery_stages = scrape_delivery_stages(page)

    return AuctionDetail(
        specification_items=specification_items,
        delivery_stages=delivery_stages,
    )


def process_purchase(page: Page, purchase, index: int, save_on_error: bool = True) -> bool:
    """Сканирует одну закупку. Возвращает True при успехе."""
    deadline = parse_deadline_from_date_range(purchase.date_range)
    print(f"\n[{index}] {purchase.auction_number}")
    print(f"  date_range: {purchase.date_range}")
    print(f"  дедлайн: {deadline}")
    print(f"  URL: {purchase.detail_url}")

    try:
        detail = scrape_auction_detail(page, purchase.detail_url)
        save_auction_detail(purchase, detail)
        print(f"  OK — позиций: {len(detail.specification_items)}, этапов: {len(detail.delivery_stages)}")
        return True
    except Exception as error:
        if save_on_error:
            save_auction_detail(purchase, AuctionDetail(), error_message=str(error))
        print(f"  ОШИБКА: {error}")
        return False


def next_batch(skipped_this_run: set[str]):
    """Очередь из БД без закупок, которые в этом запуске уже пробовали и не взяли."""
    batch = []
    for purchase in get_active_purchases_for_detail():
        if purchase.auction_number in skipped_this_run:
            continue
        batch.append(purchase)
        if len(batch) >= QUEUE_BATCH_SIZE:
            break
    return batch


def run_for_numbers(auction_numbers: list[str]) -> None:
    """
    Снимает спецификации только у переданных закупок.
    Если одну не удалось прочитать даже со второй попытки — останавливаемся.
    """
    purchases = get_purchases_by_numbers(auction_numbers)
    found_numbers = {purchase.auction_number for purchase in purchases}
    missing_numbers = [number for number in auction_numbers if number not in found_numbers]
    if missing_numbers:
        raise SystemExit(f"В БД нет карточек: {', '.join(missing_numbers)}")

    with sync_playwright() as playwright:
        browser, page = create_browser(playwright)
        try:
            for index, purchase in enumerate(purchases, start=1):
                if process_purchase(page, purchase, index, save_on_error=False):
                    pause_between_purchases()
                    continue

                print("  Перезапускаю браузер и пробую эту закупку ещё раз...")
                browser.close()
                cooldown = random.uniform(RESTART_WAIT_MIN_SEC, RESTART_WAIT_MAX_SEC)
                print(f"  ожидание {cooldown:.1f} сек")
                time.sleep(cooldown)
                browser, page = create_browser(playwright)

                if process_purchase(page, purchase, index, save_on_error=False):
                    pause_between_purchases()
                    continue

                raise SystemExit(
                    f"Не удалось снять спецификацию {purchase.auction_number}. Остановка."
                )
        finally:
            browser.close()


def main() -> None:
    success_count = 0
    skipped_count = 0
    processed_count = 0
    skipped_this_run: set[str] = set()

    with sync_playwright() as playwright:
        browser, page = create_browser(playwright)

        try:
            while True:
                batch = next_batch(skipped_this_run)
                if not batch:
                    break

                print(f"\n--- Обновление очереди из БД: {len(batch)} закупок ---")

                for purchase in batch:
                    processed_count += 1

                    if process_purchase(page, purchase, processed_count, save_on_error=False):
                        success_count += 1
                        pause_between_purchases()
                        continue

                    # Сайт уже не отдаёт спецификацию. Тот же браузер дальше почти наверняка
                    # тоже упрётся в эту ошибку — как в логе, где 17 и 18 упали подряд.
                    print("  Перезапускаю браузер и пробую эту закупку ещё раз...")
                    browser.close()
                    cooldown = random.uniform(RESTART_WAIT_MIN_SEC, RESTART_WAIT_MAX_SEC)
                    print(f"  ожидание {cooldown:.1f} сек")
                    time.sleep(cooldown)
                    browser, page = create_browser(playwright)

                    if process_purchase(page, purchase, processed_count, save_on_error=False):
                        success_count += 1
                    else:
                        skipped_count += 1
                        skipped_this_run.add(purchase.auction_number)
                        print(
                            "  Пропускаем: в БД не пишем, "
                            "в этом запуске больше не трогаем (можно снова в следующем)."
                        )

                    pause_between_purchases()
        finally:
            browser.close()

    if processed_count == 0:
        print("Нет активных закупок для обработки.")
        print("(все уже собраны, или дедлайн прошёл, или не удалось разобрать date_range)")
    else:
        print(
            f"\nИтого: успешно {success_count}, пропущено {skipped_count}, "
            f"всего попыток {processed_count}"
        )


if __name__ == "__main__":
    main()
