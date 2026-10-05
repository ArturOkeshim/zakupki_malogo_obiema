"""
Один прогон: сначала оборванные закупки, потом 10 новых.
Дальше у каждой пачки: спецификации → карточки СТЕ → агент Cursor → Excel.

Инструкция и JSON для агента — те же, что раньше для Claude
(prompts/price_research.txt и data/batch.txt).

Запуск:
    python run_pipeline.py
"""

import json

from playwright.sync_api import sync_playwright

import detail
import script
import sku_card
from ask_cursor_agent import ENV_PATH, agent_bridge, ask_on_bridge, extract_json_text, load_env_file
from export_profitable_excel import DEFAULT_OUTPUT_CLAUDE, upsert_claude_auctions
from price_research import RESPONSE_JSON_PATH, parse_response_text, write_batch_files
from price_research_repository import (
    evaluate_ready_auctions,
    get_next_pending_items,
    get_unfinished_auction_numbers,
    import_price_research_batch,
    numbers_missing_detail,
)

TENDER_LIMIT = 10
# Одна позиция за вызов агента, чтобы соседние товары не попадали в тот же промпт.
SPEC_BATCH_LIMIT = 1


def send_pending_specs(auction_numbers: list[str]) -> None:
    if not get_next_pending_items(limit=1, auction_numbers=auction_numbers):
        print("Необработанных позиций у этих закупок не осталось.")
        return

    print("Поднимаю один мост. На каждую позицию будет новый агент.")
    with agent_bridge() as (client, api_key):
        while True:
            pairs = get_next_pending_items(
                limit=SPEC_BATCH_LIMIT,
                auction_numbers=auction_numbers,
            )
            if not pairs:
                print("Необработанных позиций у этих закупок не осталось.")
                return

            sent_ids = {item.id for _, item in pairs}
            batch_path = write_batch_files(pairs)
            prompt = batch_path.read_text(encoding="utf-8")
            prompt = (
                prompt
                + "\n\nНе изменяй файлы проекта. Верни только один JSON-объект в ответе."
            )

            print("Отправляю позицию агенту...")
            answer = ask_on_bridge(client, api_key, prompt, retry_if_not_json=True)
            json_text = extract_json_text(answer)
            try:
                payload = parse_response_text(json_text)
            except json.JSONDecodeError as error:
                raw_path = RESPONSE_JSON_PATH.with_name("response_raw.txt")
                raw_path.write_text(answer, encoding="utf-8")
                raise SystemExit(
                    f"Ответ агента не JSON ({error}). Текст сохранён в {raw_path}"
                ) from error

            RESPONSE_JSON_PATH.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            updated_count, touched = import_price_research_batch(payload)
            print(f"Импорт: обновлено позиций — {updated_count}")

            still_pending = get_next_pending_items(
                limit=1000,
                auction_numbers=auction_numbers,
            )
            still_ids = {item.id for _, item in still_pending}
            if sent_ids <= still_ids:
                raise SystemExit(
                    "Агент не закрыл отправленные позиции. Остановка, в Excel не пишем."
                )

            evaluate_ready_auctions(touched)


def process_auctions(auction_numbers: list[str]) -> None:
    """Спецификация, карточка СТЕ, цена, оценка и Excel для уже известных номеров."""
    missing_detail = numbers_missing_detail(auction_numbers)
    if missing_detail:
        print("\nШаг 2. Спецификации")
        print("Номера:", ", ".join(missing_detail))
        detail.run_for_numbers(missing_detail)
    else:
        print("\nШаг 2. Спецификации уже есть, заново не снимаю")

    print("\nШаг 3. Карточки товаров на moszakupki")
    sku_card.run_for_numbers(auction_numbers)

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
