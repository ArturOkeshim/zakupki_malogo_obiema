"""
Оценка рыночных цен: экспорт батча -> Claude -> импорт -> расчёт выгодности.

Файлы (всегда одни и те же):
    data/batch.json   — данные + промпт (JSON)
    data/batch.txt    — тот же текст целиком для копирования в Claude
    data/response.json — ответ Claude для импорта

Запуск:
    python price_research.py export
    python price_research.py import
    python price_research.py evaluate
"""

import argparse
import json
from pathlib import Path

from price_evaluate import MIN_MARGIN_RUB
from price_research_batch import build_input_payload
from price_research_repository import (
    BATCH_SIZE_DEFAULT,
    evaluate_and_save_auction,
    evaluate_ready_auctions,
    get_next_pending_items,
    import_price_research_batch,
)

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
PROMPT_PATH = PROJECT_ROOT / "prompts" / "price_research.txt"
BATCH_JSON_PATH = DATA_DIR / "batch.json"
BATCH_TXT_PATH = DATA_DIR / "batch.txt"
RESPONSE_JSON_PATH = DATA_DIR / "response.json"


def load_prompt_template() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8").strip()


def build_claude_copy_paste(prompt: str, input_payload: dict) -> str:
    input_json = json.dumps(input_payload, ensure_ascii=False, indent=2)
    return f"{prompt}\n{input_json}"


def parse_response_text(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    return json.loads(text)


def write_batch_files(pairs: list) -> Path:
    """Пишет data/batch.txt: инструкция Claude и JSON позиций. Этот текст уходит агенту."""
    prompt = load_prompt_template()
    input_payload = build_input_payload(pairs)
    copy_paste = build_claude_copy_paste(prompt, input_payload)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BATCH_JSON_PATH.write_text(
        json.dumps({"prompt": prompt, "input": input_payload}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    BATCH_TXT_PATH.write_text(copy_paste, encoding="utf-8")
    print(f"Батч: {len(input_payload['items'])} позиций, id={input_payload['batch_id']}")
    return BATCH_TXT_PATH


def cmd_export(args: argparse.Namespace) -> None:
    pairs = get_next_pending_items(limit=args.limit)
    if not pairs:
        print("Нет позиций со status=pending (нужен detail_status=done у тендера).")
        return

    write_batch_files(pairs)
    print(f"Текст батча: {BATCH_TXT_PATH}")


def cmd_import(args: argparse.Namespace) -> None:
    path = Path(args.file) if args.file else RESPONSE_JSON_PATH
    if not path.is_file():
        raise SystemExit(
            f"Файл не найден: {path}\n"
            "Сохрани ответ Claude (только JSON) в data/response.json"
        )

    payload = parse_response_text(path.read_text(encoding="utf-8"))
    updated_count, auction_numbers = import_price_research_batch(payload)
    print(f"Импорт из {path}: обновлено позиций — {updated_count}")

    for auction_number in auction_numbers:
        result = evaluate_and_save_auction(auction_number)
        if result is None:
            continue
        print_report(auction_number, result)


def _fmt_money(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.2f}"


def _fmt_percent(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.1f}%"


def print_report(auction_number: str, result) -> None:
    if result.pending_count > 0:
        print(
            f"  {auction_number}: ещё не все позиции оценены "
            f"(pending={result.pending_count}), fit_status={result.fit_status}"
        )
        return

    print(f"  {auction_number}: {result.fit_status}")
    print(
        f"    NMC: {result.initial_price}, sum lines: {result.spec_sum:.2f}, "
        f"revenue: {_fmt_money(result.revenue)}"
    )

    if result.estimated_cost is None:
        print("    estimated_cost: — (не хватает данных по строкам, см. quantity / цены)")
        print(f"    not_found: {result.not_found_count}")
        return

    print(f"    estimated_cost: {result.estimated_cost:.2f}")
    print(
        f"    margin: {_fmt_money(result.margin_rub)} rub "
        f"({_fmt_percent(result.margin_percent)}), rating={result.rating}"
    )
    print(f"    not_found: {result.not_found_count}")
    if result.margin_rub is not None and result.margin_rub >= MIN_MARGIN_RUB:
        print("    -> vygodno (>= 20 000 rub)")
    elif result.margin_rub is not None:
        print("    -> nevygodno (< 20 000 rub)")


def cmd_evaluate(args: argparse.Namespace) -> None:
    if args.auction:
        result = evaluate_and_save_auction(args.auction)
        if result is None:
            print(f"Тендер {args.auction} не найден.")
            return
        print_report(args.auction, result)
        return

    results = evaluate_ready_auctions()
    if not results:
        print("Нет тендеров с detail_status=done.")
        return

    print(f"Пересчёт: {len(results)} тендер(ов)\n")
    for auction_number, result in results:
        print_report(auction_number, result)


def main() -> None:
    parser = argparse.ArgumentParser(description="Экспорт/импорт рыночных цен и расчёт выгодности")
    sub = parser.add_subparsers(dest="command", required=True)

    export_parser = sub.add_parser("export", help="Выгрузить pending позиции в data/batch.*")
    export_parser.add_argument("--limit", type=int, default=BATCH_SIZE_DEFAULT)
    export_parser.set_defaults(func=cmd_export)

    import_parser = sub.add_parser("import", help="Импорт из data/response.json")
    import_parser.add_argument(
        "file",
        nargs="?",
        default=None,
        help="По умолчанию data/response.json",
    )
    import_parser.set_defaults(func=cmd_import)

    eval_parser = sub.add_parser("evaluate", help="Пересчитать fit_status / margin / rating")
    eval_parser.add_argument("--auction", help="Только один auction_number")
    eval_parser.set_defaults(func=cmd_evaluate)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
