"""
Тот же пайплайн, что run_pipeline.py, но шаг цены идёт через pydantic-ai.

Шаги 1–3 и 5–6 не менялись: закупки, спецификации, карточки СТЕ, оценка, Excel.
Cursor SDK на этом пути не вызывается.

Запуск:
    python run_pipeline_pydantic.py
"""

import asyncio
import json
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from urllib.parse import urlparse
from zipfile import BadZipFile

from playwright.sync_api import sync_playwright

import detail
import script
import sku_card
from pydantic_ai.exceptions import ModelAPIError

from ask_pydantic_agent import (
    PRICE_SEARCH_LOG_PATH,
    ask_price,
    close_worker_browsers,
    configure_observability,
    is_market_blocked_this_run,
    mark_market_blocked_this_run,
    captcha_hosts_this_run,
    dead_hosts_for_item,
    opened_urls_for_item,
    prepare_runtime,
    reset_agent,
    reset_run_search_flags,
)
from export_profitable_excel import DEFAULT_OUTPUT_CLAUDE, export_to_excel, upsert_claude_auctions
from price_research import (
    PROMPT_PATH_PYDANTIC,
    build_agent_prompt,
    json_was_repaired,
    parse_response_text,
)
from price_search_analytics import SearchProgress, finish_run, record_item_event, start_run
from price_research_batch import (
    classify_search_kind,
    item_has_size,
    needs_search_api_first,
    search_channel_for_kind,
    web_search_query_for_item,
)
from price_research_repository import (
    RETRYABLE_FAIL_REASONS,
    evaluate_ready_auctions,
    get_api_fallback_items,
    get_next_pending_items,
    get_item_search_snapshot,
    import_price_research_batch,
    mark_item_search_failed,
    get_unfinished_auction_numbers,
    numbers_missing_detail,
)
from yandex_search_api import host_of, is_useless_price_url
from yandex_search_api import is_configured as yandex_api_configured
from yandex_search_api import probe_search_api, search_product_links

TENDER_LIMIT = 4
AGENT_CONCURRENCY = 2
PAGE_LIMIT = 5
BROWSER_WORKERS = 3
ITEM_TIMEOUT_SEC = 90


def save_agent_answer(
    item_id: int,
    auction_number: str,
    answer: str,
    agent_log: str,
    search_stage: str = "browser",
) -> bool:
    """Пишет разобранные цены и ход агента в базу. False — позиция остаётся pending."""
    try:
        payload = parse_response_text(answer)
    except json.JSONDecodeError as error:
        print(f"Ответ не JSON ({error}).")
        print(answer[:2000])
        return False

    try:
        updated_count, touched = import_price_research_batch(
            payload,
            agent_log=agent_log,
            search_stage=search_stage,
        )
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


def _note_has_403(note: str | None) -> bool:
    text = (note or "").lower()
    return "403" in text or ("доступ" in text and "запрещ" in text)


def _record_position_event(
    *,
    item,
    auction,
    item_stage: str,
    item_kind: str,
    worker_number: int,
    started_at: datetime,
    duration_sec: float,
    outcome: str,
    error_type: str | None,
    answer: str | None,
) -> dict | None:
    snapshot = get_item_search_snapshot(item.id)
    status = snapshot["status"] if snapshot else outcome
    fail_reason = snapshot["fail_reason"] if snapshot else None
    note = snapshot.get("note") if snapshot else None
    attempts = snapshot["attempts"] if snapshot else 0
    found_on_attempt = None
    if status == "priced":
        found_on_attempt = 2 if item_stage == "api" and attempts >= 1 else 1
        outcome = "priced"
    elif error_type:
        outcome = "error"
    elif status == "not_found":
        outcome = "not_found"
    record_item_event(
        {
            "specification_item_id": item.id,
            "auction_number": auction.auction_number,
            "item_name": item.name,
            "search_stage": item_stage,
            "item_kind": item_kind,
            "search_channel": (
                "web" if item_stage == "api" else search_channel_for_kind(item_kind)
            ),
            "worker_number": worker_number,
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc),
            "duration_sec": duration_sec,
            "outcome": outcome,
            "fail_reason": fail_reason,
            "error_type": error_type,
            "found_on_attempt": found_on_attempt,
            "json_repaired": bool(answer) and json_was_repaired(answer),
            "http_403": _note_has_403(note) or (answer or "").find("403") >= 0,
        }
    )
    return snapshot


def _shop_links_for_item(
    item_id: int,
    api_links_by_id: dict[int, list[dict]],
    skip_hosts: set[str],
    skip_urls: set[str] | None = None,
) -> list[dict]:
    skip_urls = {_norm_skip_url(item) for item in (skip_urls or set())}
    links = []
    for link in api_links_by_id.get(item_id) or []:
        url = str(link.get("url") or "")
        if is_useless_price_url(url):
            continue
        if _norm_skip_url(url) in skip_urls:
            continue
        host = host_of(url)
        if host and host in skip_hosts:
            continue
        links.append(link)
    return links


def _norm_skip_url(url: str) -> str:
    return str(url or "").split("#", 1)[0].rstrip("/").lower()


def resolve_search_stage(
    item,
    default_stage: str | None,
    api_links_by_id: dict,
    retrying: bool = False,
) -> str:
    if default_stage == "api":
        return "api"
    has_links = bool(api_links_by_id.get(item.id))
    if has_links and (
        needs_search_api_first(item)
        or is_market_blocked_this_run()
        or retrying
    ):
        return "api"
    if default_stage:
        return default_stage
    if getattr(item, "market_browser_attempts", 0):
        return "retry"
    return "browser"


async def price_worker(
    worker_number: int,
    pending_items: deque,
    fail_counts: dict[int, int],
    search_stage: str | None,
    api_links_by_id: dict[int, list[dict]],
    progress: SearchProgress,
) -> None:
    """Берёт следующую свободную позицию, пока очередь не кончится."""
    if worker_number > 1:
        delay = worker_number - 1
        print(f"Агент {worker_number}: пауза {delay} с")
        await asyncio.sleep(delay)
    while True:
        try:
            pair = pending_items.popleft()
        except IndexError:
            return

        auction, item = pair
        skip_hosts = dead_hosts_for_item(item.id) | captcha_hosts_this_run()
        skip_urls = opened_urls_for_item(item.id)
        item_links = _shop_links_for_item(
            item.id,
            api_links_by_id,
            skip_hosts,
            skip_urls=skip_urls,
        )
        links_for_prompt = dict(api_links_by_id)
        links_for_prompt[item.id] = item_links
        retrying = fail_counts.get(item.id, 0) > 0
        item_stage = resolve_search_stage(
            item,
            search_stage,
            links_for_prompt,
            retrying=retrying,
        )
        item_kind = classify_search_kind(item)
        has_links = bool(item_links)
        if item_stage == "api":
            plan = "api_first"
        elif has_links:
            plan = "market_then_api"
        else:
            plan = "market_only"
        page_limit = PAGE_LIMIT
        skipped = f", без {len(skip_hosts)} хост." if skip_hosts else ""
        print(
            f"Агент {worker_number}: {plan}/{item_kind}, позиция {item.id}, "
            f"закупка {auction.auction_number}, {item.name}"
            + skipped
        )
        prompt = build_agent_prompt(
            [pair],
            prompt_path=PROMPT_PATH_PYDANTIC,
            search_stage=item_stage,
            api_links_by_id=links_for_prompt,
        )
        started_at = datetime.now(timezone.utc)
        t0 = time.perf_counter()
        answer = None
        try:
            answer, agent_log = await asyncio.wait_for(
                ask_price(
                    prompt,
                    log_title=(
                        f"позиция {item.id}, закупка {auction.auction_number}, {item.name}"
                    ),
                    page_limit=page_limit,
                    worker_number=worker_number,
                    skip_hosts=skip_hosts,
                    skip_urls=skip_urls,
                ),
                timeout=ITEM_TIMEOUT_SEC,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            fail_count = fail_counts.get(item.id, 0) + 1
            fail_counts[item.id] = fail_count
            print(
                f"Агент {worker_number} не ответил за {ITEM_TIMEOUT_SEC} с: "
                f"позиция {item.id}"
            )
            duration_sec = time.perf_counter() - t0
            if fail_count >= 2:
                mark_item_search_failed(
                    item.id,
                    fail_reason="card_not_opened",
                    search_stage=item_stage,
                    note=f"Агент не ответил за {ITEM_TIMEOUT_SEC} с дважды",
                )
            _record_position_event(
                item=item,
                auction=auction,
                item_stage=item_stage,
                item_kind=item_kind,
                worker_number=worker_number,
                started_at=started_at,
                duration_sec=duration_sec,
                outcome="error",
                error_type="connection",
                answer=None,
            )
            if fail_count >= 2:
                print(
                    f"Позиция {item.id} пропускается в этом заходе. "
                    "Агент берёт следующую."
                )
                progress.note_finished(
                    duration_sec,
                    may_need_api=item_stage != "api",
                )
                continue
            pending_items.append(pair)
            print(f"Позиция {item.id} снова в очереди. Агент берёт следующую.")
            progress.note_finished(duration_sec, requeued=True)
            continue
        except Exception as error:
            fail_count = fail_counts.get(item.id, 0) + 1
            fail_counts[item.id] = fail_count
            print(f"Агент {worker_number} оборвался: {error}")
            if not isinstance(error, ModelAPIError):
                traceback.print_exc()
            duration_sec = time.perf_counter() - t0
            if fail_count >= 2:
                mark_item_search_failed(
                    item.id,
                    fail_reason="card_not_opened",
                    search_stage=item_stage,
                    note=f"Агент оборвался дважды: {error}",
                )
            _record_position_event(
                item=item,
                auction=auction,
                item_stage=item_stage,
                item_kind=item_kind,
                worker_number=worker_number,
                started_at=started_at,
                duration_sec=duration_sec,
                outcome="error",
                error_type="connection" if isinstance(error, ModelAPIError) else "other",
                answer=None,
            )
            if fail_count >= 2:
                print(
                    f"Позиция {item.id} пропускается в этом запуске. "
                    "Агент берёт следующую."
                )
                progress.note_finished(
                    duration_sec,
                    may_need_api=item_stage != "api",
                )
                continue
            if isinstance(error, ModelAPIError):
                print("  Жду 3 с и пробую эту позицию снова (связь с OpenRouter).")
                await asyncio.sleep(3)
            pending_items.append(pair)
            print(f"Позиция {item.id} снова в очереди. Агент берёт следующую.")
            progress.note_finished(duration_sec, requeued=True)
            continue

        saved = save_agent_answer(
            item.id,
            auction.auction_number,
            answer,
            agent_log,
            search_stage=item_stage,
        )
        duration_sec = time.perf_counter() - t0
        snapshot = _record_position_event(
            item=item,
            auction=auction,
            item_stage=item_stage,
            item_kind=item_kind,
            worker_number=worker_number,
            started_at=started_at,
            duration_sec=duration_sec,
            outcome="pending" if not saved else "priced",
            error_type=None if saved else "json",
            answer=answer,
        )
        fail_reason = snapshot["fail_reason"] if snapshot else None
        if snapshot and snapshot.get("http_403"):
            mark_market_blocked_this_run()
        progress.note_finished(
            duration_sec,
            may_need_api=(
                item_stage != "api" and fail_reason in RETRYABLE_FAIL_REASONS
            ),
        )
        if not saved:
            print(
                f"Агент {worker_number}: позиция {item.id} остаётся pending, "
                "беру следующую."
            )


async def search_pending_prices(
    pairs: list,
    search_stage: str | None = None,
    api_links_by_id: dict[int, list[dict]] | None = None,
    progress: SearchProgress | None = None,
) -> None:
    pending_items = deque(pairs)
    worker_count = AGENT_CONCURRENCY
    if worker_count > len(pending_items):
        worker_count = len(pending_items)
    links = api_links_by_id or {}

    print(f"В очереди позиций: {len(pending_items)}. Одновременно агентов: {worker_count}.")
    print(f"Лог поиска: {PRICE_SEARCH_LOG_PATH}")
    # Пайплайн вызывает asyncio.run несколько раз. Клиент OpenRouter нельзя переносить.
    reset_agent()
    fail_counts: dict[int, int] = {}
    tracker = progress or SearchProgress(concurrency=worker_count)
    workers = [
        asyncio.create_task(
            price_worker(
                number,
                pending_items,
                fail_counts,
                search_stage,
                links,
                tracker,
            )
        )
        for number in range(1, worker_count + 1)
    ]
    try:
        await asyncio.gather(*workers)
    finally:
        await close_worker_browsers()
        logfire_flush()


def logfire_flush() -> None:
    try:
        import logfire

        logfire.force_flush()
    except Exception:
        return


def collect_api_links(pairs: list, avoid_market: bool = False) -> dict[int, list[dict]]:
    links_by_id: dict[int, list[dict]] = {}
    for _, item in pairs:
        kind = classify_search_kind(item)
        query = web_search_query_for_item(item, kind)
        print(f"  Search API, позиция {item.id} ({kind}): {query}")
        try:
            links = search_product_links(
                query,
                kind=kind,
                avoid_market=avoid_market,
                has_size=item_has_size(item),
            )
        except Exception as error:
            print(f"  Search API ошибка для позиции {item.id}: {error}")
            links = []
        links_by_id[item.id] = links
        hosts = []
        for link in links:
            host = urlparse(link["url"]).netloc
            if host and host not in hosts:
                hosts.append(host)
        host_text = f" ({', '.join(hosts[:5])})" if hosts else ""
        print(f"  ссылок: {len(links)}{host_text}")
    return links_by_id


def _first_pass_api_links(pairs: list) -> dict[int, list[dict]]:
    """Ссылки заранее: услугам — сразу, товару — запас в том же заходе, если Маркет 403."""
    if not yandex_api_configured():
        print("Search API не настроен: все позиции идут в браузер без готовых ссылок")
        return {}
    api_first = sum(1 for _, item in pairs if needs_search_api_first(item))
    print(
        f"Search API заранее для {len(pairs)} позиций "
        f"({api_first} сразу по ссылкам, {len(pairs) - api_first} — запас после Маркета)"
    )
    return collect_api_links(pairs, avoid_market=True)


def _second_pass_pairs(auction_numbers: list[str]) -> list:
    """Капча/карточка не открылась или позиция осталась pending после обрыва связи."""
    merged = []
    seen: set[int] = set()
    for pair in get_api_fallback_items(auction_numbers) + get_next_pending_items(
        limit=10000,
        auction_numbers=auction_numbers,
    ):
        item_id = pair[1].id
        if item_id in seen:
            continue
        seen.add(item_id)
        merged.append(pair)
    return merged


def send_pending_specs(auction_numbers: list[str]) -> None:
    if AGENT_CONCURRENCY < 1:
        raise SystemExit("AGENT_CONCURRENCY должен быть не меньше 1.")

    reset_run_search_flags()
    start_run(auction_numbers, AGENT_CONCURRENCY)
    progress = SearchProgress(
        concurrency=AGENT_CONCURRENCY,
        forecast_api=yandex_api_configured(),
    )
    status = "done"
    try:
        pairs = get_next_pending_items(limit=10000, auction_numbers=auction_numbers)
        if not pairs:
            print("Необработанных позиций у этих закупок не осталось.")
        else:
            print("Ищу цены через pydantic-ai. Мост Cursor не поднимаю.")
            market_count = sum(
                1
                for _, item in pairs
                if search_channel_for_kind(classify_search_kind(item)) == "market"
            )
            web_count = len(pairs) - market_count
            print(
                f"Заход 1. Один заход на позицию. Маркет, затем ссылки API: {market_count} тов. "
                f"Сразу ссылки API: {web_count} услуг и позиций без Маркета"
            )
            first_links = _first_pass_api_links(pairs)
            with_links = sum(1 for links in first_links.values() if links)
            if with_links:
                print(f"Search API сразу подставил ссылки {with_links} позициям")
            progress.begin_pass("заход 1", len(pairs))
            if not _run_price_search(
                pairs,
                api_links_by_id=first_links,
                progress=progress,
            ):
                status = "interrupted"
                return

        api_pairs = _second_pass_pairs(auction_numbers)
        if not api_pairs:
            progress.confirm_no_second_pass()
            return
        if not yandex_api_configured():
            print(
                "Заход 2. Search API пропущен: добавьте YANDEX_SEARCH_API_KEY "
                "и YANDEX_FOLDER_ID в .env"
            )
            progress.confirm_no_second_pass()
            return
        print(
            f"Заход 2. Только кто не дошёл до ссылок API: {len(api_pairs)} "
            "(капча на Маркете без магазинов или обрыв)"
        )
        api_links = collect_api_links(api_pairs, avoid_market=True)
        progress.begin_pass("заход 2", len(api_pairs))
        if not _run_price_search(
            api_pairs,
            search_stage="api",
            api_links_by_id=api_links,
            progress=progress,
        ):
            status = "interrupted"
    finally:
        progress.close()
        finish_run(status)


def _run_price_search(
    pairs: list,
    search_stage: str | None = None,
    api_links_by_id: dict[int, list[dict]] | None = None,
    progress: SearchProgress | None = None,
) -> bool:
    """False — поиск оборвали с клавиатуры, следующие заходы не запускаем."""
    try:
        asyncio.run(
            search_pending_prices(
                pairs,
                search_stage=search_stage,
                api_links_by_id=api_links_by_id,
                progress=progress,
            )
        )
    except KeyboardInterrupt:
        print("Поиск цен остановлен. Уже сохранённые позиции остаются в базе.")
        return False
    return True


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
    try:
        saved = upsert_claude_auctions(DEFAULT_OUTPUT_CLAUDE, auction_numbers)
        print(f"В таблице обновлено тендеров: {saved}")
        print(f"Файл: {DEFAULT_OUTPUT_CLAUDE.resolve()}")
    except PermissionError:
        fallback = DEFAULT_OUTPUT_CLAUDE.with_name("tenders_claude_unlocked.xlsx")
        saved = upsert_claude_auctions(fallback, auction_numbers)
        print(f"Основной Excel занят, записал {saved} тендеров в {fallback.resolve()}")
    except BadZipFile:
        saved = export_to_excel(DEFAULT_OUTPUT_CLAUDE, "claude")
        print(f"Файл Excel был битый, перезаписал из базы: {saved} тендеров")
        print(f"Файл: {DEFAULT_OUTPUT_CLAUDE.resolve()}")


def main() -> None:
    prepare_runtime()
    configure_observability()
    probe = probe_search_api()
    if probe["ok"]:
        print(
            f"Search API проверка: {probe['count']} ссылок, "
            f"хосты {', '.join(probe['hosts'][:6])}"
        )
    else:
        print(f"Search API проверка не прошла: {probe['error']}")

    unfinished = get_unfinished_auction_numbers()
    if unfinished:
        print("Оборванные закупки, сначала доделываю их:")
        print(", ".join(unfinished))
        process_auctions(unfinished)
        return

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
