"""
Аналитика поиска цен (шаг 4 pydantic-пайплайна).

После прогона смотреть:
    data/analytics/latest.json
    data/analytics/run_<id>.json
    python price_search_analytics.py

Пока поиск идёт:
    data/analytics/progress.json
    строка «--- заход 1 [████░░░░] 8/24 33% | прошло … | осталось ~… ---»

События пишутся в JSONL по ходу, сводка — в конце шага 4 и в таблицы
price_search_runs / price_search_item_events.
"""

from __future__ import annotations

import json
import statistics
import sys
import threading
import time
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from db.models import PriceSearchItemEventRecord, PriceSearchRunRecord
from db.session import SessionLocal
from price_research_repository import snapshot_items_for_auctions

PROJECT_ROOT = Path(__file__).resolve().parent
ANALYTICS_DIR = PROJECT_ROOT / "data" / "analytics"
EVENTS_JSONL_PATH = ANALYTICS_DIR / "events.jsonl"
LATEST_JSON_PATH = ANALYTICS_DIR / "latest.json"
PROGRESS_JSON_PATH = ANALYTICS_DIR / "progress.json"
SCHEMA_VERSION = 1
_BAR_WIDTH = 20
_RECENT_ATTEMPTS = 8

_lock = threading.Lock()
_current_run_id: int | None = None
_current_auctions: list[str] = []
_search_started_at: datetime | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone().isoformat(timespec="seconds")


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    total = max(0, int(round(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours} ч {minutes} мин"
    if minutes:
        if secs == 0:
            return f"{minutes} мин"
        return f"{minutes} мин {secs} с"
    return f"{secs} с"


def _seed_sec_per_item() -> float | None:
    summary = load_latest_summary()
    if not summary:
        return None
    timing = summary.get("timing") or {}
    value = timing.get("avg_wall_sec_per_item")
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return None


def _supports_block_bar() -> bool:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        "█░".encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


def render_bar(done: int, total: int, width: int = _BAR_WIDTH) -> str:
    if total <= 0:
        filled = 0
    else:
        filled = min(width, int(round(width * done / total)))
    empty = width - filled
    if _supports_block_bar():
        return f"[{'█' * filled}{'░' * empty}]"
    return f"[{'#' * filled}{'-' * empty}]"


class SearchProgress:
    """Живой прогресс текущего поиска: полоска, ETA, progress.json."""

    def __init__(self, *, concurrency: int, forecast_api: bool = False) -> None:
        self.concurrency = max(int(concurrency), 1)
        self.forecast_api = forecast_api
        self._lock = threading.Lock()
        self.total = 0
        self.done = 0
        self.expected_extra = 0
        self.pass_name = ""
        self.started = time.perf_counter()
        self.seed_wall_sec = _seed_sec_per_item()
        self._recent_attempts: deque[float] = deque(maxlen=_RECENT_ATTEMPTS)

    def begin_pass(self, name: str, count: int) -> None:
        if count <= 0:
            return
        with self._lock:
            self.pass_name = name
            if self.total == 0:
                self.total = count
            else:
                self.total = self.done + count
                self.expected_extra = 0
        print(f"{name}: в очереди {count} поз.")
        self.render()

    def confirm_no_second_pass(self) -> None:
        with self._lock:
            changed = self.expected_extra != 0
            self.expected_extra = 0
        if changed:
            self.render()

    def note_finished(
        self,
        duration_sec: float,
        *,
        requeued: bool = False,
        may_need_api: bool = False,
    ) -> None:
        with self._lock:
            self._recent_attempts.append(max(float(duration_sec), 0.0))
            if not requeued:
                self.done += 1
                if self.forecast_api and may_need_api:
                    self.expected_extra += 1
        self.render()

    def snapshot(self) -> dict:
        elapsed = time.perf_counter() - self.started
        remaining_items = max(self.total - self.done, 0) + self.expected_extra
        known_total = self.total + self.expected_extra
        avg_attempt = (
            sum(self._recent_attempts) / len(self._recent_attempts)
            if self._recent_attempts
            else None
        )
        eta_source = "unknown"
        eta_sec: float | None = None
        wall_per_item: float | None = None
        if remaining_items <= 0:
            eta_sec = 0.0
            eta_source = "done"
            wall_per_item = elapsed / self.done if self.done else None
        elif avg_attempt is not None:
            workers = min(self.concurrency, remaining_items)
            eta_sec = remaining_items * avg_attempt / workers
            wall_per_item = avg_attempt / self.concurrency
            eta_source = "current"
        elif self.seed_wall_sec is not None:
            eta_sec = remaining_items * self.seed_wall_sec
            wall_per_item = self.seed_wall_sec
            eta_source = "previous"
        percent = round(100.0 * self.done / known_total, 1) if known_total else 100.0
        return {
            "run_id": _current_run_id,
            "pass": self.pass_name,
            "done": self.done,
            "total": known_total,
            "queued": max(self.total - self.done, 0),
            "forecast_api": self.expected_extra,
            "elapsed_sec": round(elapsed, 1),
            "eta_sec": None if eta_sec is None else round(eta_sec, 1),
            "sec_per_item_wall": (
                None if wall_per_item is None else round(wall_per_item, 1)
            ),
            "eta_source": eta_source,
            "percent": min(percent, 100.0),
            "updated_at": _iso(_now()),
        }

    def render(self) -> None:
        data = self.snapshot()
        self._write_progress_file(data)
        print(self._format_line(data), flush=True)

    def close(self) -> None:
        if self.total == 0 and self.done == 0:
            return
        self.render()

    def _format_line(self, data: dict) -> str:
        bar = render_bar(int(data["done"]), int(data["total"] or 1))
        elapsed = format_duration(data["elapsed_sec"])
        eta_sec = data.get("eta_sec")
        source = data.get("eta_source")
        if eta_sec is None:
            remaining = "осталось ?"
        elif source == "done" or eta_sec <= 0:
            remaining = "осталось ~0 с"
        else:
            remaining = f"осталось ~{format_duration(eta_sec)}"
            if source == "previous":
                remaining += " (по прошлому прогону)"
        extra = ""
        if data.get("forecast_api"):
            extra = f"  (+{data['forecast_api']} в заход 2)"
        pace = data.get("sec_per_item_wall")
        pace_text = f"  ~{format_duration(pace)}/поз" if pace else ""
        label = data.get("pass") or "поиск"
        return (
            f"--- {label} {bar} {data['done']}/{data['total']} "
            f"{data['percent']:.0f}% | прошло {elapsed} | {remaining}"
            f"{pace_text}{extra} ---"
        )

    def _write_progress_file(self, data: dict) -> None:
        try:
            ANALYTICS_DIR.mkdir(parents=True, exist_ok=True)
            PROGRESS_JSON_PATH.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            return


def start_run(auction_numbers: list[str], concurrency: int) -> int:
    """Открывает прогон поиска. Возвращает run_id."""
    global _current_run_id, _current_auctions, _search_started_at
    started = _now()
    with SessionLocal() as session:
        run = PriceSearchRunRecord(
            started_at=started,
            status="running",
            concurrency=concurrency,
            auction_numbers_json=json.dumps(auction_numbers, ensure_ascii=False),
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        run_id = run.id
    _current_run_id = run_id
    _current_auctions = list(auction_numbers)
    _search_started_at = started
    ANALYTICS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Аналитика прогона {run_id}: {ANALYTICS_DIR / f'run_{run_id}.json'}")
    return run_id


def current_run_id() -> int | None:
    return _current_run_id


def record_item_event(event: dict) -> None:
    """Один заход агента. Пишет JSONL сразу, чтобы Ctrl+C не стёр факт."""
    run_id = event.get("run_id") or _current_run_id
    if run_id is None:
        return
    payload = dict(event)
    payload["run_id"] = run_id
    ANALYTICS_DIR.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, default=str)
    with _lock:
        with EVENTS_JSONL_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        with SessionLocal() as session:
            session.add(
                PriceSearchItemEventRecord(
                    run_id=run_id,
                    specification_item_id=int(payload["specification_item_id"]),
                    auction_number=str(payload["auction_number"]),
                    item_name=payload.get("item_name"),
                    search_stage=str(payload["search_stage"]),
                    item_kind=str(payload["item_kind"]),
                    search_channel=str(payload["search_channel"]),
                    worker_number=int(payload.get("worker_number") or 1),
                    started_at=payload["started_at"],
                    finished_at=payload["finished_at"],
                    duration_sec=float(payload["duration_sec"]),
                    outcome=str(payload["outcome"]),
                    fail_reason=payload.get("fail_reason"),
                    error_type=payload.get("error_type"),
                    found_on_attempt=payload.get("found_on_attempt"),
                    json_repaired=bool(payload.get("json_repaired")),
                    http_403=bool(payload.get("http_403")),
                )
            )
            session.commit()


def _load_events(run_id: int) -> list[dict]:
    with SessionLocal() as session:
        rows = session.scalars(
            select(PriceSearchItemEventRecord)
            .where(PriceSearchItemEventRecord.run_id == run_id)
            .order_by(PriceSearchItemEventRecord.id)
        ).all()
        return [
            {
                "specification_item_id": row.specification_item_id,
                "auction_number": row.auction_number,
                "item_name": row.item_name,
                "search_stage": row.search_stage,
                "item_kind": row.item_kind,
                "search_channel": row.search_channel,
                "worker_number": row.worker_number,
                "duration_sec": row.duration_sec,
                "outcome": row.outcome,
                "fail_reason": row.fail_reason,
                "error_type": row.error_type,
                "found_on_attempt": row.found_on_attempt,
                "json_repaired": row.json_repaired,
                "http_403": row.http_403,
            }
            for row in rows
        ]


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    return round(float(statistics.median(values)), 2)


def build_summary(
    run_id: int,
    *,
    status: str,
    started_at: datetime,
    finished_at: datetime,
    search_duration_sec: float,
    auction_numbers: list[str],
    concurrency: int,
) -> dict:
    events = _load_events(run_id)
    items = snapshot_items_for_auctions(auction_numbers)
    statuses = Counter(item["status"] for item in items)
    priced = statuses.get("priced", 0)
    not_found = statuses.get("not_found", 0)
    pending = statuses.get("pending", 0)
    processed = priced + not_found
    reasons = Counter(
        item["fail_reason"] or "unknown"
        for item in items
        if item["status"] == "not_found"
    )
    attempt_hits = Counter()
    for event in events:
        if event["outcome"] == "priced" and event.get("found_on_attempt"):
            attempt_hits[str(event["found_on_attempt"])] += 1
    attempt_values = [
        event["found_on_attempt"]
        for event in events
        if event["outcome"] == "priced" and event.get("found_on_attempt")
    ]
    durations = [float(event["duration_sec"]) for event in events]
    last_by_item: dict[int, dict] = {}
    for event in events:
        last_by_item[event["specification_item_id"]] = event
    by_kind: dict[str, dict[str, int]] = defaultdict(
        lambda: {"priced": 0, "not_found": 0, "pending": 0, "error": 0}
    )
    by_channel: dict[str, dict[str, int]] = defaultdict(
        lambda: {"priced": 0, "not_found": 0, "pending": 0, "error": 0}
    )
    for event in last_by_item.values():
        by_kind[event["item_kind"]][event["outcome"]] += 1
        by_channel[event["search_channel"]][event["outcome"]] += 1
    errors = Counter(event["error_type"] for event in events if event.get("error_type"))
    http_403 = sum(1 for event in events if event.get("http_403"))
    json_repaired = sum(1 for event in events if event.get("json_repaired"))
    worst = sorted(events, key=lambda event: event["duration_sec"], reverse=True)[:8]
    unique_items = {item["id"] for item in items} or {event["specification_item_id"] for event in events}
    unique_auctions = set(auction_numbers) or {event["auction_number"] for event in events}

    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "status": status,
        "started_at": _iso(started_at),
        "finished_at": _iso(finished_at),
        "concurrency": concurrency,
        "auction_numbers": auction_numbers,
        "search_duration_sec": round(search_duration_sec, 1),
        "timing": {
            "avg_wall_sec_per_item": (
                round(search_duration_sec / len(unique_items), 1) if unique_items else None
            ),
            "avg_wall_sec_per_auction": (
                round(search_duration_sec / len(unique_auctions), 1) if unique_auctions else None
            ),
            "avg_agent_sec": _mean(durations),
            "median_agent_sec": _median(durations),
            "agent_runs": len(events),
        },
        "funnel": {
            "items": len(items) or len(unique_items),
            "auctions": len(unique_auctions),
            "priced": priced,
            "not_found": not_found,
            "pending": pending,
            "processed": processed,
            "priced_rate": round(priced / processed, 3) if processed else None,
        },
        "not_found_reasons": dict(reasons),
        "found_on_attempt": {
            "counts": dict(attempt_hits),
            "avg": _mean([float(value) for value in attempt_values]),
        },
        "by_kind": dict(by_kind),
        "by_channel": dict(by_channel),
        "errors": {
            **dict(errors),
            "http_403": http_403,
            "json_repaired": json_repaired,
        },
        "worst_items": [
            {
                "specification_item_id": event["specification_item_id"],
                "auction_number": event["auction_number"],
                "name": event.get("item_name"),
                "duration_sec": round(event["duration_sec"], 1),
                "outcome": event["outcome"],
                "fail_reason": event.get("fail_reason"),
                "search_stage": event["search_stage"],
            }
            for event in worst
        ],
        "files": {
            "latest": str(LATEST_JSON_PATH),
            "run": str(ANALYTICS_DIR / f"run_{run_id}.json"),
            "events": str(EVENTS_JSONL_PATH),
        },
    }


def _write_summary_files(summary: dict) -> None:
    ANALYTICS_DIR.mkdir(parents=True, exist_ok=True)
    text = json.dumps(summary, ensure_ascii=False, indent=2)
    run_path = ANALYTICS_DIR / f"run_{summary['run_id']}.json"
    run_path.write_text(text, encoding="utf-8")
    LATEST_JSON_PATH.write_text(text, encoding="utf-8")


def print_summary(summary: dict) -> None:
    funnel = summary["funnel"]
    timing = summary["timing"]
    found = summary["found_on_attempt"]
    print("\n--- Аналитика поиска ---")
    print(
        f"Прогон {summary['run_id']}: {summary['search_duration_sec']} с, "
        f"статус {summary['status']}"
    )
    print(
        f"Закупок {funnel['auctions']}, позиций {funnel['items']}: "
        f"priced {funnel['priced']}, not_found {funnel['not_found']}, "
        f"pending {funnel['pending']}"
    )
    if funnel.get("priced_rate") is not None:
        print(f"Точность (priced / обработанные): {funnel['priced_rate']:.0%}")
    print(
        f"Среднее на позицию (стена): {timing['avg_wall_sec_per_item']} с, "
        f"на закупку: {timing['avg_wall_sec_per_auction']} с, "
        f"агент median {timing['median_agent_sec']} с"
    )
    if found.get("counts"):
        print(
            f"Найден с попытки: {found['counts']}, среднее {found['avg']}"
        )
    if summary["not_found_reasons"]:
        print(f"not_found причины: {summary['not_found_reasons']}")
    errors = {key: value for key, value in summary["errors"].items() if value}
    if errors:
        print(f"Ошибки: {errors}")
    print(f"Сводка: {LATEST_JSON_PATH}")


def finish_run(status: str = "done") -> dict | None:
    """Закрывает прогон, пишет JSON и печатает сводку."""
    global _current_run_id, _search_started_at
    run_id = _current_run_id
    if run_id is None:
        return None
    finished = _now()
    started = _search_started_at or finished
    duration = max((finished - started).total_seconds(), 0.0)
    with SessionLocal() as session:
        run = session.get(PriceSearchRunRecord, run_id)
        concurrency = run.concurrency if run is not None else 1
        auctions = list(_current_auctions)
        if run is not None:
            try:
                auctions = json.loads(run.auction_numbers_json)
            except json.JSONDecodeError:
                auctions = list(_current_auctions)
        summary = build_summary(
            run_id,
            status=status,
            started_at=started,
            finished_at=finished,
            search_duration_sec=duration,
            auction_numbers=auctions,
            concurrency=concurrency,
        )
        if run is not None:
            run.finished_at = finished
            run.status = status
            run.search_duration_sec = duration
            run.item_count = summary["funnel"]["items"]
            run.priced_count = summary["funnel"]["priced"]
            run.not_found_count = summary["funnel"]["not_found"]
            run.pending_count = summary["funnel"]["pending"]
            run.summary_json = json.dumps(summary, ensure_ascii=False)
            session.commit()
    _write_summary_files(summary)
    print_summary(summary)
    _current_run_id = None
    _search_started_at = None
    return summary


def load_latest_summary() -> dict | None:
    if LATEST_JSON_PATH.is_file():
        return json.loads(LATEST_JSON_PATH.read_text(encoding="utf-8"))

    with SessionLocal() as session:
        run = session.scalars(
            select(PriceSearchRunRecord).order_by(PriceSearchRunRecord.id.desc())
        ).first()
        if run is None or not run.summary_json:
            return None
        return json.loads(run.summary_json)


def main() -> None:
    summary = load_latest_summary()
    if summary is None:
        print("Сводки ещё нет. Запустите python run_pipeline_pydantic.py")
        return
    print_summary(summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
