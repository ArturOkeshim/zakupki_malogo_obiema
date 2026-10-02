"""
Yandex «Товары» (products_mode): export -> Claude в браузере -> import -> расчёт по медиане.

Файлы:
    data/batch_yandex.txt
    data/batch_yandex.json
    data/response_yandex.json

Запуск:
    python price_research_yandex.py export
    python price_research_yandex.py import   # после импорта автоматически evaluate
    python price_research_yandex.py evaluate   # пересчёт вручную (все тендеры или --auction)
"""

import argparse
import json
from pathlib import Path

from price_evaluate import MIN_MARGIN_RUB
from price_research_batch import build_input_payload
from price_research_yandex_repository import (
    BATCH_SIZE_DEFAULT,
    evaluate_ready_auctions_yandex,
    get_next_yandex_pending_items,
    import_yandex_batch,
)

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
PROMPT_PATH = PROJECT_ROOT / "prompts" / "price_research_yandex.txt"
BATCH_JSON_PATH = DATA_DIR / "batch_yandex.json"
BATCH_TXT_PATH = DATA_DIR / "batch_yandex.txt"
RESPONSE_JSON_PATH = DATA_DIR / "response_yandex.json"


def load_prompt_template() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8").strip()


def build_copy_paste(prompt: str, input_payload: dict) -> str:
    input_json = json.dumps(input_payload, ensure_ascii=False, indent=2)
    return f"{prompt}\n{input_json}"


def parse_response_text(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    return json.loads(text)


def specification_ids_in_batch_json(path: Path) -> set[int]:
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    input_block = data.get("input") if isinstance(data, dict) else None
    if not isinstance(input_block, dict):
        return set()
    items = input_block.get("items")
    if not isinstance(items, list):
        return set()
    result: set[int] = set()
    for row in items:
        if isinstance(row, dict) and row.get("specification_item_id") is not None:
            result.add(int(row["specification_item_id"]))
    return result


def specification_ids_in_response_file(path: Path) -> set[int]:
    if not path.is_file():
        return set()
    try:
        payload = parse_response_text(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return set()
    items = payload.get("items")
    if not isinstance(items, list):
        return set()
    result: set[int] = set()
    for row in items:
        if not isinstance(row, dict):
            continue
        spec_id = row.get("specification_item_id")
        if spec_id is not None:
            result.add(int(spec_id))
    return result


def cmd_export(args: argparse.Namespace) -> None:
    pairs = get_next_yandex_pending_items(limit=args.limit)
    if not pairs:
        print("Нет позиций с yandex_research_status=pending.")
        return

    export_ids = {item.id for _, item in pairs}
    response_ids = specification_ids_in_response_file(RESPONSE_JSON_PATH)
    overlap = sorted(export_ids & response_ids)
    if overlap and not args.force:
        preview = ", ".join(str(i) for i in overlap[:8])
        if len(overlap) > 8:
            preview += f", … (+{len(overlap) - 8})"
        raise SystemExit(
            f"В {RESPONSE_JSON_PATH.name} уже есть ответ для {len(overlap)} позиций, "
            f"но в БД они всё ещё pending (specification_item_id: {preview}).\n"
            "Сначала выполни: python price_research_yandex.py import\n"
            "Если нужно перезаписать batch для Claude без import: export --force"
        )

    previous_batch_ids = specification_ids_in_batch_json(BATCH_JSON_PATH)
    if (
        previous_batch_ids
        and previous_batch_ids == export_ids
        and not args.force
    ):
        raise SystemExit(
            "Следующий батч совпадает с прошлым export (те же specification_item_id).\n"
            "Обычно это значит: ответ Claude ещё не импортирован, или в response_yandex.json "
            "лежит JSON от другого батча.\n"
            "После Claude: сохрани ответ в data/response_yandex.json и выполни import.\n"
            "Повторный export без import: export --force"
        )

    prompt = load_prompt_template()
    input_payload = build_input_payload(pairs)
    copy_paste = build_copy_paste(prompt, input_payload)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BATCH_JSON_PATH.write_text(
        json.dumps({"prompt": prompt, "input": input_payload}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    BATCH_TXT_PATH.write_text(copy_paste, encoding="utf-8")

    print(f"Экспорт Yandex: {len(input_payload['items'])} позиций")
    print(f"batch_id: {input_payload['batch_id']}")
    print(f"Копируй в Claude: {BATCH_TXT_PATH}")
    print("После Claude: сохрани JSON в data/response_yandex.json")
    print("Затем обязательно: python price_research_yandex.py import  (иначе export снова выберет те же позиции)")


def run_yandex_evaluate(auction_numbers: list[str] | None = None) -> None:
    """Тот же пересчёт, что команда evaluate (fit_status_yandex, margin, rating)."""
    results = evaluate_ready_auctions_yandex(auction_numbers)
    if not results:
        if auction_numbers:
            print("Пересчёт Yandex: нет данных по указанным тендерам.")
        else:
            print("Нет тендеров с detail_status=done.")
        return

    scope = f"{len(results)} тендер(ов)"
    if auction_numbers:
        scope = f"{len(results)} тендер(ов) из импорта"
    print(f"Пересчёт Yandex: {scope}\n")
    for auction_number, result in results:
        print_report(auction_number, result)


def cmd_import(args: argparse.Namespace) -> None:
    path = Path(args.file) if args.file else RESPONSE_JSON_PATH
    if not path.is_file():
        raise SystemExit(f"Файл не найден: {path}")

    payload = parse_response_text(path.read_text(encoding="utf-8"))
    updated_count, auction_numbers = import_yandex_batch(payload)
    print(f"Импорт Yandex: обновлено позиций — {updated_count}")

    # ЗАМЕТКА ДЛЯ ОБУЧЕНИЯ: import меняет цены в БД; evaluate считает маржу и fit_status.
    print()
    run_yandex_evaluate(auction_numbers if auction_numbers else None)


def _fmt_money(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def print_report(auction_number: str, result) -> None:
    if result.pending_count > 0:
        print(
            f"  {auction_number}: pending={result.pending_count}, "
            f"fit_status_yandex={result.fit_status}"
        )
        return

    print(f"  {auction_number}: fit_status_yandex={result.fit_status}")
    print(f"    revenue: {_fmt_money(result.revenue)}, cost (median): {_fmt_money(result.estimated_cost)}")
    if result.estimated_cost is None:
        print(f"    not_found: {result.not_found_count}")
        return
    print(
        f"    margin: {_fmt_money(result.margin_rub)} rub, "
        f"rating_yandex={result.rating}, not_found={result.not_found_count}"
    )
    if result.margin_rub is not None and result.margin_rub >= MIN_MARGIN_RUB:
        print("    -> vygodno (>= 20 000 rub, median)")


def cmd_evaluate(args: argparse.Namespace) -> None:
    if args.auction:
        run_yandex_evaluate([args.auction])
        return
    run_yandex_evaluate(None)


def main() -> None:
    parser = argparse.ArgumentParser(description="Yandex Товары: export/import/evaluate")
    sub = parser.add_subparsers(dest="command", required=True)

    export_p = sub.add_parser("export")
    export_p.add_argument("--limit", type=int, default=BATCH_SIZE_DEFAULT)
    export_p.add_argument(
        "--force",
        action="store_true",
        help="экспорт даже если response_yandex.json уже содержит эти pending-позиции",
    )
    export_p.set_defaults(func=cmd_export)

    import_p = sub.add_parser("import")
    import_p.add_argument("file", nargs="?", default=None)
    import_p.set_defaults(func=cmd_import)

    eval_p = sub.add_parser("evaluate")
    eval_p.add_argument("--auction", default=None)
    eval_p.set_defaults(func=cmd_evaluate)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
