"""
Один прогон: сначала оборванные закупки, потом 10 новых.
Дальше у каждой пачки: спецификации → карточки СТЕ → агент Cursor → Excel.

Сколько агентов ищут цены одновременно — константа AGENT_CONCURRENCY.
Сколько адресов может открыть один агент — PAGE_LIMIT.
Сколько браузеров снимают спецификации и карточки — BROWSER_WORKERS.
Ход поиска пишется в data/price_search_log.txt.
Промпт и ответ живут в памяти, на диск не пишутся.

Запуск:
    python run_pipeline.py
"""

import asyncio
import json
from collections import deque
import traceback

from playwright.sync_api import sync_playwright

import detail
import script
import sku_card
from ask_cursor_agent import (
    ENV_PATH,
    PRICE_SEARCH_LOG_PATH,
    async_agent_bridge,
    ask_on_async_bridge,
    extract_json_text,
    load_env_file,
)
from export_profitable_excel import DEFAULT_OUTPUT_CLAUDE, upsert_claude_auctions
from price_research import build_agent_prompt, parse_response_text
from price_research_repository import (
    evaluate_ready_auctions,
    get_next_pending_items,
    get_unfinished_auction_numbers,
    import_price_research_batch,
    numbers_missing_detail,
)

TENDER_LIMIT = 1
# Сколько агентов одновременно ищут цены. У каждого в промпте одна позиция.
AGENT_CONCURRENCY = 3
# Сколько адресов один агент может открыть. Следующий переход обрывается.
PAGE_LIMIT = 5
# Сколько браузеров одновременно снимают спецификации и карточки товаров.
BROWSER_WORKERS = 3


def save_agent_answer(
    item_id: int,
    auction_number: str,
    answer: str,
    agent_log: str,
) -> bool:
    """Пишет разобранные цены и ход агента в базу. False — позиция остаётся pending."""
    json_text = extract_json_text(answer)
    try:
        payload = parse_response_text(json_text)
    except json.JSONDecodeError as error:
        print(f"Ответ не JSON ({error}).")
        print(answer[:2000])
        return False

    try:
        updated_count, touched = import_price_research_batch(payload, agent_log=agent_log)
    except ValueError as error:
        print(f"Ответ не удалось записать: {error}")
        return False

    print(f"Импорт: обновлено позиций — {updated_count}")
    still_pending = get_next_pending_items(
        limit=10000,
        auction_numbers=[auction_number],
    )
    still_ids = {item.id for _, item in still_pending}
    if item_id in still_ids:
        print(f"Позиция {item_id} в ответе не закрыта.")
        return False

    evaluate_ready_auctions(touched)
    return True


async def price_worker(
    worker_number: int,
    client,
    api_key: str,
    pending_items: deque,
    bridge_fail_counts: dict[int, int],
    start_lock: asyncio.Lock,
) -> None:
    """Берёт следующую свободную позицию, пока очередь не кончится."""
    while True:
        # ЗАМЕТКА ДЛЯ ОБУЧЕНИЯ:
        # popleft выполняется сразу, без ожидания. Пока один агент забрал
        # позицию, второй не может взять её же — очередь одна на всех.
        try:
            pair = pending_items.popleft()
        except IndexError:
            return

        auction, item = pair
        print(
            f"Агент {worker_number}: позиция {item.id}, "
            f"закупка {auction.auction_number}, {item.name}"
        )
        prompt = build_agent_prompt([pair])
        try:
            answer, agent_log = await ask_on_async_bridge(
                client,
                api_key,
                prompt,
                log_path=PRICE_SEARCH_LOG_PATH,
                log_title=(
                    f"позиция {item.id}, закупка {auction.auction_number}, {item.name}"
                ),
                page_limit=PAGE_LIMIT,
                start_lock=start_lock,
            )
        except Exception as error:
            # Мост оборвался (например ReadError). Этот агент не выключаем:
            # позицию возвращаем в очередь и берём следующую.
            # Два таких обрыва на одной позиции — в этом запуске её больше не берём,
            # иначе агенты будут падать на ней по кругу.
            fail_count = bridge_fail_counts.get(item.id, 0) + 1
            bridge_fail_counts[item.id] = fail_count
            print(f"Агент {worker_number} оборвался: {error}")
            traceback.print_exc()
            if fail_count >= 2:
                print(
                    f"Позиция {item.id} пропускается в этом запуске. "
                    "Агент берёт следующую."
                )
                continue
            pending_items.append(pair)
            print(f"Позиция {item.id} снова в очереди. Агент берёт следующую.")
            continue

        saved = save_agent_answer(item.id, auction.auction_number, answer, agent_log)
        if not saved:
            print(
                f"Агент {worker_number}: позиция {item.id} остаётся pending, "
                "беру следующую."
            )


async def search_pending_prices(pairs: list) -> None:
    pending_items = deque(pairs)
    worker_count = AGENT_CONCURRENCY
    if worker_count > len(pending_items):
        worker_count = len(pending_items)

    print(f"В очереди позиций: {len(pending_items)}. Одновременно агентов: {worker_count}.")
    print("Запуск агентов по одному. Поиск после запуска идёт параллельно.")
    print(f"Лог поиска: {PRICE_SEARCH_LOG_PATH}")
    # Общий счёт обрывов моста на позицию. Его видят все агенты.
    bridge_fail_counts: dict[int, int] = {}
    # Один замок на все перезапуски: новый агент создаётся, только когда предыдущий уже работает.
    agent_start_lock = asyncio.Lock()
    async with async_agent_bridge() as (client, api_key):
        # ЗАМЕТКА ДЛЯ ОБУЧЕНИЯ:
        # create_task запускает функцию и не ждёт её конца.
        # gather ждёт, пока закончат все агенты, но друг друга они не ждут.
        # Очередь запуска — agent_start_lock внутри ask_on_async_bridge.
        workers = []
        for worker_number in range(1, worker_count + 1):
            task = asyncio.create_task(
                price_worker(
                    worker_number,
                    client,
                    api_key,
                    pending_items,
                    bridge_fail_counts,
                    agent_start_lock,
                )
            )
            workers.append(task)
        await asyncio.gather(*workers)


def send_pending_specs(auction_numbers: list[str]) -> None:
    # У функции нет номера страницы: один запрос забирает весь список pending.
    pairs = get_next_pending_items(limit=10000, auction_numbers=auction_numbers)
    if not pairs:
        print("Необработанных позиций у этих закупок не осталось.")
        return
    if AGENT_CONCURRENCY < 1:
        raise SystemExit("AGENT_CONCURRENCY должен быть не меньше 1.")

    print("Поднимаю один мост. Агенты берут позиции из общей очереди.")
    asyncio.run(search_pending_prices(pairs))


def process_auctions(auction_numbers: list[str]) -> None:
    """Спецификация, карточка СТЕ, цена, оценка и Excel для уже известных номеров."""
    missing_detail = numbers_missing_detail(auction_numbers)
    if missing_detail:
        print("\nШаг 2. Спецификации")
        print("Номера:", ", ".join(missing_detail))
        detail.run_for_numbers(missing_detail, worker_count=BROWSER_WORKERS)
    else:
        print("\nШаг 2. Спецификации уже есть, заново не снимаю")

    print("\nШаг 3. Карточки товаров на moszakupki")
    sku_card.run_for_numbers(auction_numbers, worker_count=BROWSER_WORKERS)

    print("\nШаг 4. Рыночные цены через локального агента")
    send_pending_specs(auction_numbers)

    print("\nШаг 5. Оценка выгодности")
    evaluate_ready_auctions(auction_numbers)

    print("\nШаг 6. Excel")
    saved = upsert_claude_auctions(DEFAULT_OUTPUT_CLAUDE, auction_numbers)
    print(f"В таблице обновлено тендеров: {saved}")
    print(f"Файл: {DEFAULT_OUTPUT_CLAUDE.resolve()}")


def main() -> None:
    load_env_file(ENV_PATH)

    unfinished = get_unfinished_auction_numbers()
    if unfinished:
        print("Оборванные закупки, сначала доделываю их:")
        print(", ".join(unfinished))
        process_auctions(unfinished)
    else:
        print("Оборванных закупок нет.")

    print(f"\nШаг 1. Ищу {TENDER_LIMIT} новых закупок на сайте")
    with sync_playwright() as playwright:
        purchases = script.run(playwright, max_new=TENDER_LIMIT)

    if len(purchases) < TENDER_LIMIT:
        raise SystemExit(
            f"Новых закупок нашлось {len(purchases)}, нужно {TENDER_LIMIT}. Остановка."
        )

    auction_numbers = [purchase.auction_number for purchase in purchases]
    print("Номера:", ", ".join(auction_numbers))
    process_auctions(auction_numbers)


if __name__ == "__main__":
    main()
