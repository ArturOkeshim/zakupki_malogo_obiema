import json
from datetime import datetime, timezone

from sqlalchemy import and_, select

from db.models import AuctionRecord, PurchaseRecord, SpecificationItemRecord
from db.session import SessionLocal
from price_evaluate import (
    EvaluateResult,
    SpecLineForEvaluate,
    evaluate_auction,
    tender_line_total,
)

# Одна позиция на запуск агента: чужие строки в том же промпте сбивают поиск.
BATCH_SIZE_DEFAULT = 1
RETRYABLE_FAIL_REASONS = frozenset({"captcha", "card_not_opened"})
KNOWN_FAIL_REASONS = frozenset({"captcha", "card_not_opened", "no_match", "api_no_card"})


def api_fallback_clause():
    """Search API: после одного браузерного провала капчи или «карточка не открылась»."""
    return and_(
        SpecificationItemRecord.price_research_status == "not_found",
        SpecificationItemRecord.market_fail_reason.in_(RETRYABLE_FAIL_REASONS),
        SpecificationItemRecord.market_browser_attempts >= 1,
        SpecificationItemRecord.market_api_used.is_(False),
    )


def infer_fail_reason(row: dict) -> str:
    explicit = row.get("fail_reason")
    if explicit in KNOWN_FAIL_REASONS:
        return explicit
    note = f"{row.get('note') or ''} {json.dumps(row.get('sources') or [], ensure_ascii=False)}"
    lowered = note.lower()
    if (
        "капч" in lowered
        or "smartcaptcha" in lowered
        or "я не робот" in lowered
        or "403" in lowered
        or "доступ" in lowered and "запрещ" in lowered
    ):
        return "captcha"
    if (
        "не откры" in lowered
        or "snapshot" in lowered
        or "клик" in lowered
        or "карточк" in lowered and "не" in lowered
    ):
        return "card_not_opened"
    return "no_match"


def get_unfinished_auction_numbers() -> list[str]:
    """
    Закупки, которые пайплайн уже сохранил, но не довёл до цены.
    Это нет спецификации или есть позиции со статусом pending.
    Уже оценённые закупки сюда не входят, кроме not_found,
    которые ещё ждут Search API после капчи или неоткрытой карточки.
    """
    with SessionLocal() as session:
        purchases = session.scalars(
            select(PurchaseRecord).order_by(PurchaseRecord.scraped_at)
        ).all()

        latest_scraped_at = {}
        for purchase in purchases:
            latest_scraped_at[purchase.auction_number] = purchase.scraped_at

        done_numbers = set(
            session.scalars(
                select(AuctionRecord.auction_number).where(AuctionRecord.detail_status == "done")
            ).all()
        )
        pending_numbers = set(
            session.scalars(
                select(SpecificationItemRecord.auction_number)
                .where(SpecificationItemRecord.price_research_status == "pending")
                .distinct()
            ).all()
        )
        retry_numbers = set(
            session.scalars(
                select(SpecificationItemRecord.auction_number)
                .where(api_fallback_clause())
                .distinct()
            ).all()
        )

    unfinished = []
    for auction_number, scraped_at in latest_scraped_at.items():
        detail_missing = auction_number not in done_numbers
        price_missing = auction_number in pending_numbers or auction_number in retry_numbers
        if detail_missing or price_missing:
            unfinished.append((scraped_at, auction_number))

    unfinished.sort()
    return [auction_number for _, auction_number in unfinished]


def numbers_missing_detail(auction_numbers: list[str]) -> list[str]:
    """Из списка оставляет номера, у которых спецификация ещё не снята."""
    if not auction_numbers:
        return []

    with SessionLocal() as session:
        done_numbers = set(
            session.scalars(
                select(AuctionRecord.auction_number).where(
                    AuctionRecord.auction_number.in_(auction_numbers),
                    AuctionRecord.detail_status == "done",
                )
            ).all()
        )

    return [number for number in auction_numbers if number not in done_numbers]


def get_next_pending_items(
    limit: int = BATCH_SIZE_DEFAULT,
    auction_numbers: list[str] | None = None,
) -> list[tuple[AuctionRecord, SpecificationItemRecord]]:
    """
    Следующие limit позиций со status=pending.
    Сначала тендеры с detail_status=done (свежее detail_scraped_at — раньше в очереди).
    """
    with SessionLocal() as session:
        auctions = session.scalars(
            select(AuctionRecord)
            .where(AuctionRecord.detail_status == "done")
            .order_by(AuctionRecord.detail_scraped_at.desc().nulls_last())
        ).all()
        if auction_numbers is not None:
            allowed = set(auction_numbers)
            auctions = [auction for auction in auctions if auction.auction_number in allowed]

        result: list[tuple[AuctionRecord, SpecificationItemRecord]] = []
        # ЗАМЕТКА ДЛЯ ОБУЧЕНИЯ: один тендер может дать несколько позиций в батче.
        # expunge для одного и того же auction делаем только один раз.
        expunged_auction_numbers: set[str] = set()

        for auction in auctions:
            items = session.scalars(
                select(SpecificationItemRecord)
                .where(
                    SpecificationItemRecord.auction_number == auction.auction_number,
                    SpecificationItemRecord.price_research_status == "pending",
                )
                .order_by(SpecificationItemRecord.position_index)
            ).all()

            for item in items:
                if auction.auction_number not in expunged_auction_numbers:
                    session.expunge(auction)
                    expunged_auction_numbers.add(auction.auction_number)
                session.expunge(item)
                result.append((auction, item))
                if len(result) >= limit:
                    return result

        return result


def load_pairs_by_item_ids(item_ids: list[int]) -> list[tuple[AuctionRecord, SpecificationItemRecord]]:
    if not item_ids:
        return []
    with SessionLocal() as session:
        items = session.scalars(
            select(SpecificationItemRecord).where(SpecificationItemRecord.id.in_(item_ids))
        ).all()
        by_id = {item.id: item for item in items}
        auction_numbers = {item.auction_number for item in items}
        auctions = session.scalars(
            select(AuctionRecord).where(AuctionRecord.auction_number.in_(auction_numbers))
        ).all()
        auction_map = {auction.auction_number: auction for auction in auctions}
        expunged: set[str] = set()
        result: list[tuple[AuctionRecord, SpecificationItemRecord]] = []
        for item_id in item_ids:
            item = by_id.get(item_id)
            if item is None:
                continue
            auction = auction_map.get(item.auction_number)
            if auction is None:
                continue
            if auction.auction_number not in expunged:
                session.expunge(auction)
                expunged.add(auction.auction_number)
            session.expunge(item)
            result.append((auction, item))
        return result


def requeue_retryable_browser(
    auction_numbers: list[str] | None = None,
) -> list[tuple[AuctionRecord, SpecificationItemRecord]]:
    """not_found после первого захода из‑за капчи или клика — снова pending."""
    with SessionLocal() as session:
        query = select(SpecificationItemRecord.id).where(
            SpecificationItemRecord.price_research_status == "not_found",
            SpecificationItemRecord.market_fail_reason.in_(RETRYABLE_FAIL_REASONS),
            SpecificationItemRecord.market_browser_attempts == 1,
            SpecificationItemRecord.market_api_used.is_(False),
        )
        if auction_numbers is not None:
            query = query.where(SpecificationItemRecord.auction_number.in_(auction_numbers))
        item_ids = list(session.scalars(query).all())
        if not item_ids:
            return []
        items = session.scalars(
            select(SpecificationItemRecord).where(SpecificationItemRecord.id.in_(item_ids))
        ).all()
        for item in items:
            item.price_research_status = "pending"
        session.commit()
    return load_pairs_by_item_ids(item_ids)


def mark_item_search_failed(
    item_id: int,
    *,
    fail_reason: str,
    search_stage: str,
    note: str,
) -> None:
    """Два обрыва агента: позиция не pending, чтобы Search API / сводка её видели."""
    reason = fail_reason if fail_reason in KNOWN_FAIL_REASONS else "card_not_opened"
    with SessionLocal() as session:
        item = session.get(SpecificationItemRecord, item_id)
        if item is None:
            return
        if search_stage == "api":
            item.market_api_used = True
            if reason == "card_not_opened":
                reason = "api_no_card"
        else:
            item.market_browser_attempts = (item.market_browser_attempts or 0) + 1
        item.price_research_status = "not_found"
        item.market_fail_reason = reason
        item.market_confidence = "not_found"
        item.market_note = note
        session.commit()


def get_api_fallback_items(
    auction_numbers: list[str] | None = None,
) -> list[tuple[AuctionRecord, SpecificationItemRecord]]:
    """Капча или карточка не открылась с первого браузера — очередь на Search API."""
    with SessionLocal() as session:
        query = select(SpecificationItemRecord.id).where(api_fallback_clause())
        if auction_numbers is not None:
            query = query.where(SpecificationItemRecord.auction_number.in_(auction_numbers))
        item_ids = list(session.scalars(query).all())
    return load_pairs_by_item_ids(item_ids)


def _sources_left_market(row: dict) -> bool:
    from yandex_search_api import is_useless_price_url

    for source in row.get("sources") or []:
        if not isinstance(source, dict):
            continue
        url = str(source.get("url") or "")
        lowered = url.lower()
        if not lowered.startswith("http") or "market.yandex.ru" in lowered:
            continue
        if is_useless_price_url(url):
            continue
        return True
    return False


def import_price_research_batch(
    payload: dict,
    agent_log: str | None = None,
    search_stage: str = "browser",
) -> tuple[int, list[str]]:
    """
    Сохраняет ответ нейросети. Возвращает (число обновлённых строк, auction_number для пересчёта).

    agent_log — ход этого прогона: размышления, действия и вывод.
    Его нет, когда JSON пришёл из файла, а не от агента.
    """
    items_payload = payload.get("items")
    if not isinstance(items_payload, list):
        raise ValueError("В JSON нет массива items")

    analyzed_at_raw = payload.get("analyzed_at")
    if analyzed_at_raw:
        analyzed_at = datetime.fromisoformat(analyzed_at_raw.replace("Z", "+00:00"))
    else:
        analyzed_at = datetime.now(timezone.utc)

    updated_count = 0
    auction_numbers: set[str] = set()

    with SessionLocal() as session:
        for row in items_payload:
            spec_id = row.get("specification_item_id")
            if spec_id is None:
                raise ValueError("В items нет specification_item_id")

            item = session.get(SpecificationItemRecord, int(spec_id))
            if item is None:
                raise ValueError(f"specification_item_id={spec_id} не найден в БД")

            confidence = row.get("confidence") or "not_found"
            market_price = row.get("market_price_per_unit")

            if confidence == "not_found" or market_price is None:
                item.price_research_status = "not_found"
                item.market_price_per_unit = None
                item.market_fail_reason = infer_fail_reason(row)
                if search_stage == "api" and item.market_fail_reason == "card_not_opened":
                    item.market_fail_reason = "api_no_card"
            else:
                item.price_research_status = "priced"
                item.market_price_per_unit = float(market_price)
                item.market_fail_reason = None

            if search_stage in {"browser", "retry"}:
                item.market_browser_attempts = (item.market_browser_attempts or 0) + 1
            if search_stage == "api" or _sources_left_market(row):
                item.market_api_used = True

            item.market_confidence = confidence
            item.market_item_type = row.get("item_type")
            item.market_note = row.get("note")
            if agent_log:
                item.market_agent_log = agent_log
            sources = row.get("sources")
            if sources is not None:
                item.market_sources_json = json.dumps(sources, ensure_ascii=False)
            item.market_analyzed_at = analyzed_at

            auction_numbers.add(item.auction_number)
            updated_count += 1

        session.commit()

    return updated_count, sorted(auction_numbers)


def _lines_for_auction(session, auction_number: str) -> list[SpecLineForEvaluate]:
    items = session.scalars(
        select(SpecificationItemRecord)
        .where(SpecificationItemRecord.auction_number == auction_number)
        .order_by(SpecificationItemRecord.position_index)
    ).all()

    lines: list[SpecLineForEvaluate] = []
    for item in items:
        line_total = tender_line_total(
            float(item.total_price) if item.total_price is not None else None,
            float(item.price_per_unit) if item.price_per_unit is not None else None,
            float(item.quantity) if item.quantity is not None else None,
        )
        lines.append(
            SpecLineForEvaluate(
                specification_item_id=item.id,
                price_research_status=item.price_research_status,
                tender_line_total=line_total,
                quantity=float(item.quantity) if item.quantity is not None else None,
                market_price_per_unit=(
                    float(item.market_price_per_unit)
                    if item.market_price_per_unit is not None
                    else None
                ),
            )
        )
    return lines


def evaluate_and_save_auction(auction_number: str) -> EvaluateResult | None:
    with SessionLocal() as session:
        auction = session.get(AuctionRecord, auction_number)
        if auction is None:
            return None

        initial_price = float(auction.initial_price) if auction.initial_price is not None else None
        lines = _lines_for_auction(session, auction_number)
        result = evaluate_auction(initial_price, lines)

        auction.fit_status = result.fit_status
        auction.margin_rub = result.margin_rub
        auction.margin_percent = result.margin_percent
        auction.rating = result.rating

        if result.pending_count == 0 and lines:
            auction.analysis_status = "done"

        session.commit()
        return result


def evaluate_ready_auctions(auction_numbers: list[str] | None = None) -> list[tuple[str, EvaluateResult]]:
    """
    Пересчёт fit_status. Если auction_numbers не передан — все detail_status=done.
    """
    results: list[tuple[str, EvaluateResult]] = []

    with SessionLocal() as session:
        if auction_numbers:
            numbers = auction_numbers
        else:
            numbers = list(
                session.scalars(
                    select(AuctionRecord.auction_number).where(AuctionRecord.detail_status == "done")
                ).all()
            )

    for auction_number in numbers:
        result = evaluate_and_save_auction(auction_number)
        if result is not None:
            results.append((auction_number, result))

    return results


def get_item_search_snapshot(item_id: int) -> dict | None:
    with SessionLocal() as session:
        item = session.get(SpecificationItemRecord, item_id)
        if item is None:
            return None
        return {
            "id": item.id,
            "name": item.name,
            "auction_number": item.auction_number,
            "status": item.price_research_status,
            "fail_reason": item.market_fail_reason,
            "attempts": item.market_browser_attempts or 0,
            "api_used": bool(item.market_api_used),
            "note": item.market_note,
        }


def snapshot_items_for_auctions(auction_numbers: list[str]) -> list[dict]:
    if not auction_numbers:
        return []
    with SessionLocal() as session:
        items = session.scalars(
            select(SpecificationItemRecord).where(
                SpecificationItemRecord.auction_number.in_(auction_numbers)
            )
        ).all()
        return [
            {
                "id": item.id,
                "name": item.name,
                "auction_number": item.auction_number,
                "status": item.price_research_status,
                "fail_reason": item.market_fail_reason,
                "attempts": item.market_browser_attempts or 0,
                "api_used": bool(item.market_api_used),
            }
            for item in items
        ]
