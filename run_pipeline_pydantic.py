"""
Тот же пайплайн, что run_pipeline.py, но шаг цены идёт через pydantic-ai.

Шаги 1–3 и 5–6 не менялись: закупки, спецификации, карточки СТЕ, оценка, Excel.
Cursor SDK на этом пути не вызывается.

Запуск:
    python run_pipeline_pydantic.py
"""

import asyncio
import json
from collections import deque
import traceback

from playwright.sync_api import sync_playwright

import detail
import script
import sku_card
from ask_pydantic_agent import (
    PRICE_SEARCH_LOG_PATH,
    ask_price,
    configure_observability,
    prepare_runtime,
    reset_agent,
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

TENDER_LIMIT = 10
AGENT_CONCURRENCY = 3
PAGE_LIMIT = 5
BROWSER_WORKERS = 3


def save_agent_answer(
    item_id: int,
    auction_number: str,
    answer: str,
    agent_log: str,
) -> bool:
    """Пишет разобранные цены и ход агента в базу. False — позиция остаётся pending."""
    try:
        payload = parse_response_text(answer)
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
    pending_items: deque,
    fail_counts: dict[int, int],
) -> None:
    """Берёт следующую свободную позицию, пока очередь не кончится."""
    if worker_number > 1:
        delay = (worker_number - 1) * 3
        print(f"Агент {worker_number}: пауза {delay} с, чтобы не бить Маркет одновременно")
        await asyncio.sleep(delay)
    while True:
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
            answer, agent_log = await ask_price(
                prompt,
                log_title=(
                    f"позиция {item.id}, закупка {auction.auction_number}, {item.name}"
                ),
                page_limit=PAGE_LIMIT,
            )
        except Exception as error:
            fail_count = fail_counts.get(item.id, 0) + 1
            fail_counts[item.id] = fail_count
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
    print(f"Лог поиска: {PRICE_SEARCH_LOG_PATH}")
    # Пайплайн вызывает asyncio.run дважды: оборванные закупки, потом новые.
    # Клиент OpenRouter и asyncio.Lock нельзя переносить в новый цикл.
    reset_agent()
    fail_counts: dict[int, int] = {}
    workers = [
        asyncio.create_task(price_worker(number, pending_items, fail_counts))
        for number in range(1, worker_count + 1)
    ]
    await asyncio.gather(*workers)
    logfire_flush()


def logfire_flush() -> None:
    try:
        import logfire

        logfire.force_flush()
    except Exception:
        return


def send_pending_specs(auction_numbers: list[str]) -> None:
    pairs = get_next_pending_items(limit=10000, auction_numbers=auction_numbers)
    if not pairs:
        print("Необработанных позиций у этих закупок не осталось.")
        return
    if AGENT_CONCURRENCY < 1:
        raise SystemExit("AGENT_CONCURRENCY должен быть не меньше 1.")

    print("Ищу цены через pydantic-ai. Мост Cursor не поднимаю.")
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

    print("\nШаг 4. Рыночные цены через pydantic-ai")
    send_pending_specs(auction_numbers)

    print("\nШаг 5. Оценка выгодности")
    evaluate_ready_auctions(auction_numbers)

    print("\nШаг 6. Excel")
    saved = upsert_claude_auctions(DEFAULT_OUTPUT_CLAUDE, auction_numbers)
    print(f"В таблице обновлено тендеров: {saved}")
    print(f"Файл: {DEFAULT_OUTPUT_CLAUDE.resolve()}")


def main() -> None:
    prepare_runtime()
    configure_observability()

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
