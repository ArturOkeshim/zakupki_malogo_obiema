"""
Цена из HTML до браузера.

Search API уже дал ссылки магазинов. Если в разметке страницы есть цена и тот же
артикул, браузер и модель не нужны. Совпавший артикул кэшируется на несколько дней.
Услуги и страницы без артикула сюда не попадают: их по-прежнему смотрит агент.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from pydantic import BaseModel

from price_research_batch import _is_model_code
from yandex_search_api import host_of

PROJECT_ROOT = Path(__file__).resolve().parent
CACHE_PATH = PROJECT_ROOT / "data" / "price_cache.json"
CACHE_TTL = timedelta(days=7)
FETCH_LIMIT = 6
FETCH_TIMEOUT_SEC = 8.0
MAX_HTML_CHARS = 1_500_000

_CACHE_LOCK = threading.Lock()
_ARTICLE_TOKEN = re.compile(r"[A-Za-zА-Яа-я0-9][A-Za-zА-Яа-я0-9._/-]{3,23}")
_LOOKALIKE = str.maketrans(
    {
        "а": "a",
        "е": "e",
        "о": "o",
        "р": "p",
        "с": "c",
        "у": "y",
        "х": "x",
        "к": "k",
        "м": "m",
        "т": "t",
        "в": "b",
        "н": "h",
        "ё": "e",
    }
)
_LD_JSON = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_PRICE_ATTRS = (
    re.compile(
        r'itemprop=["\']price["\'][^>]*content=["\']([^"\']+)["\']',
        re.IGNORECASE,
    ),
    re.compile(
        r'content=["\']([^"\']+)["\'][^>]*itemprop=["\']price["\']',
        re.IGNORECASE,
    ),
    re.compile(
        r'property=["\']product:price:amount["\'][^>]*content=["\']([^"\']+)["\']',
        re.IGNORECASE,
    ),
)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_CAPTCHA_MARKERS = ("smartcaptcha", "showcaptcha", "проверка пользователя", "я не робот")
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class HttpPriceSource(BaseModel):
    title: str
    url: str
    price_seen: float
    note: str


class HttpPriceItem(BaseModel):
    specification_item_id: int
    item_type: str = "goods"
    market_price_per_unit: float
    currency: str = "RUB"
    confidence: str = "high"
    fail_reason: None = None
    sources: list[HttpPriceSource]
    note: str


class HttpPriceAnswer(BaseModel):
    batch_id: str = "http-fetch"
    analyzed_at: str
    items: list[HttpPriceItem]


def normalize_article(text: str) -> str:
    """Одинаковый ключ для РА16-003B и PA16-003B."""
    lowered = str(text or "").lower().replace("ё", "е").translate(_LOOKALIKE)
    return re.sub(r"[^a-z0-9]", "", lowered)


def article_codes(spec_item) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    model = str(getattr(spec_item, "sku_model", "") or "").strip()
    name = str(getattr(spec_item, "name", "") or "")
    candidates = [model] if model else []
    candidates.extend(_ARTICLE_TOKEN.findall(name))
    if model:
        candidates.extend(_ARTICLE_TOKEN.findall(model))
    for token in candidates:
        if not _is_model_code(token):
            continue
        key = normalize_article(token)
        if len(key) < 4 or key in seen:
            continue
        seen.add(key)
        found.append(token)
    return found


def _parse_price(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if 0 < number < 50_000_000:
            return round(number, 2)
        return None
    text = str(value or "").replace("\xa0", " ").strip()
    text = re.sub(r"[^\d,.\s]", "", text).replace(" ", "")
    if not text:
        return None
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        tail = text.split(",")[-1]
        text = text.replace(",", ".") if len(tail) == 2 else text.replace(",", "")
    try:
        number = float(text)
    except ValueError:
        return None
    if not 0 < number < 50_000_000:
        return None
    return round(number, 2)


def _flat(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(_flat(item) for item in value)
    if isinstance(value, dict):
        return _flat(value.get("name") or value.get("@value") or "")
    return str(value).strip()


def _code_in_text(code_key: str, hay: str) -> bool:
    if len(code_key) < 4 or code_key not in hay:
        return False
    if code_key.isdigit():
        return re.search(rf"(?<!\d){re.escape(code_key)}(?!\d)", hay) is not None
    return True


def _offers_from_jsonld(node, page_url: str, found: list[dict], depth: int = 0) -> None:
    if depth > 8:
        return
    if isinstance(node, list):
        for item in node:
            _offers_from_jsonld(item, page_url, found, depth + 1)
        return
    if not isinstance(node, dict):
        return
    if "@graph" in node:
        _offers_from_jsonld(node["@graph"], page_url, found, depth + 1)
    name = _flat(node.get("name"))
    sku = " ".join(
        part
        for part in (
            _flat(node.get("sku")),
            _flat(node.get("mpn")),
            _flat(node.get("productID")),
        )
        if part
    )
    offers = node.get("offers")
    if isinstance(offers, dict):
        offers = [offers]
    if isinstance(offers, list):
        for offer in offers:
            if not isinstance(offer, dict):
                continue
            currency = str(offer.get("priceCurrency") or "RUB").upper()
            if currency not in {"RUB", "RUR"}:
                continue
            price = _parse_price(offer.get("price") or offer.get("lowPrice"))
            if price is None:
                continue
            found.append(
                {
                    "name": name or _flat(offer.get("name")),
                    "sku": sku,
                    "price": price,
                    "url": _flat(offer.get("url")) or page_url,
                    "title": name or sku or page_url,
                }
            )
    for value in node.values():
        if isinstance(value, (dict, list)):
            _offers_from_jsonld(value, page_url, found, depth + 1)


def _visible_title(html: str) -> str:
    match = _TITLE.search(html)
    if not match:
        return ""
    return _TAG.sub(" ", match.group(1))


def extract_offers(html: str, page_url: str) -> list[dict]:
    found: list[dict] = []
    for block in _LD_JSON.findall(html):
        try:
            payload = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        _offers_from_jsonld(payload, page_url, found)
    if found:
        return _dedupe_offers(found)
    title = " ".join(_visible_title(html).split())
    for pattern in _PRICE_ATTRS:
        match = pattern.search(html)
        if not match:
            continue
        price = _parse_price(match.group(1))
        if price is None:
            continue
        found.append(
            {
                "name": title,
                "sku": title,
                "price": price,
                "url": page_url,
                "title": title or page_url,
            }
        )
        break
    return found


def _dedupe_offers(offers: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    unique: list[dict] = []
    for offer in offers:
        key = (offer["url"], offer["price"], normalize_article(offer["sku"] + offer["name"]))
        if key in seen:
            continue
        seen.add(key)
        unique.append(offer)
    return unique


def _pack_conflicts(unit: str, text: str) -> bool:
    if str(unit or "").strip().lower() not in {"шт", "штука", "штук"}:
        return False
    match = re.search(r"(\d+)\s*шт", text.lower())
    return bool(match and int(match.group(1)) > 1)


def matching_offer(spec_item, offers: list[dict]) -> tuple[str, dict] | None:
    codes = article_codes(spec_item)
    unit = str(getattr(spec_item, "unit", "") or "")
    if not codes:
        return None
    for offer in offers:
        blob = f"{offer.get('name') or ''} {offer.get('sku') or ''}"
        if _pack_conflicts(unit, blob):
            continue
        hay = normalize_article(blob)
        for code in codes:
            if _code_in_text(normalize_article(code), hay):
                return code, offer
    return None


def _cache_key(spec_item) -> str | None:
    codes = article_codes(spec_item)
    if not codes:
        return None
    unit = str(getattr(spec_item, "unit", "") or "").strip().lower()
    return f"{normalize_article(codes[0])}|{unit}"


def _load_cache() -> dict:
    if not CACHE_PATH.is_file():
        return {}
    try:
        payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_cache(payload: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _answer_from_offer(spec_item, code: str, offer: dict, via: str) -> str:
    unit = str(getattr(spec_item, "unit", "") or "ед.")
    price = float(offer["price"])
    note = (
        f"{via}: артикул {code} совпал с карточкой. "
        f"Цена {price} за {unit}, без пересчёта упаковки."
    )
    answer = HttpPriceAnswer(
        analyzed_at=datetime.now(timezone.utc).astimezone().isoformat(),
        items=[
            HttpPriceItem(
                specification_item_id=int(spec_item.id),
                market_price_per_unit=price,
                sources=[
                    HttpPriceSource(
                        title=(offer.get("title") or code)[:180],
                        url=str(offer["url"]),
                        price_seen=price,
                        note="Цена из разметки страницы, артикул совпал.",
                    )
                ],
                note=note,
            )
        ],
    )
    return json.dumps(answer.model_dump(), ensure_ascii=False)


def _remember(spec_item, code: str, offer: dict) -> None:
    key = _cache_key(spec_item)
    if not key:
        return
    row = {
        "code": code,
        "price": offer["price"],
        "url": offer["url"],
        "title": offer.get("title") or "",
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    with _CACHE_LOCK:
        payload = _load_cache()
        payload[key] = row
        _save_cache(payload)


def _cached_answer(spec_item) -> str | None:
    key = _cache_key(spec_item)
    if not key:
        return None
    with _CACHE_LOCK:
        row = _load_cache().get(key)
    if not isinstance(row, dict):
        return None
    saved_at = str(row.get("saved_at") or "")
    try:
        moment = datetime.fromisoformat(saved_at)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - moment > CACHE_TTL:
        return None
    price = _parse_price(row.get("price"))
    url = str(row.get("url") or "")
    if price is None or not url.startswith("http"):
        return None
    offer = {
        "price": price,
        "url": url,
        "title": str(row.get("title") or ""),
        "name": str(row.get("title") or ""),
        "sku": str(row.get("code") or ""),
    }
    return _answer_from_offer(spec_item, str(row.get("code") or ""), offer, "Кэш артикула")


def _looks_like_captcha(html: str) -> bool:
    head = html[:4000].lower()
    return any(marker in head for marker in _CAPTCHA_MARKERS)


def remember_agent_price(spec_item, answer: str) -> None:
    """После браузера запоминаем цену, только если в ответе есть тот же артикул."""
    codes = article_codes(spec_item)
    if not codes or not answer:
        return
    try:
        from price_research import parse_response_text

        payload = parse_response_text(answer)
    except (json.JSONDecodeError, ValueError, TypeError):
        return
    rows = payload.get("items") if isinstance(payload, dict) else None
    if not rows:
        return
    row = rows[0]
    if row.get("confidence") != "high" or row.get("market_price_per_unit") is None:
        return
    blob = json.dumps(row, ensure_ascii=False)
    code = next((item for item in codes if normalize_article(item) in normalize_article(blob)), None)
    if code is None:
        return
    sources = row.get("sources") or []
    url = ""
    title = code
    if sources and isinstance(sources[0], dict):
        url = str(sources[0].get("url") or "")
        title = str(sources[0].get("title") or code)
    if not url.startswith("http"):
        return
    price = _parse_price(row["market_price_per_unit"])
    if price is None:
        return
    _remember(spec_item, code, {"price": price, "url": url, "title": title})


async def _fetch_html(client: httpx.AsyncClient, url: str) -> str | None:
    try:
        response = await client.get(url)
    except httpx.HTTPError:
        return None
    if response.status_code >= 400:
        return None
    content_type = response.headers.get("content-type", "")
    if content_type and "html" not in content_type and "text" not in content_type:
        return None
    text = response.text
    if len(text) > MAX_HTML_CHARS:
        text = text[:MAX_HTML_CHARS]
    if _looks_like_captcha(text):
        host = host_of(url)
        if host:
            from ask_pydantic_agent import mark_captcha_host

            mark_captcha_host(host)
        return None
    return text


async def lookup_http_price(spec_item, links: list[dict]) -> tuple[str, str] | None:
    """(json, лог) если артикул нашёлся в разметке или в кэше. Иначе None."""
    if not article_codes(spec_item):
        return None
    cached = _cached_answer(spec_item)
    if cached:
        log = f"\n=== HTTP cache | позиция {spec_item.id} ===\n{cached}\n"
        return cached, log

    urls = []
    for link in links:
        url = str(link.get("url") or "")
        host = host_of(url)
        if not url.startswith("http") or "market.yandex.ru" in host:
            continue
        urls.append(url)
        if len(urls) >= FETCH_LIMIT:
            break
    if not urls:
        return None

    timeout = httpx.Timeout(FETCH_TIMEOUT_SEC, connect=5.0)
    headers = {"User-Agent": _USER_AGENT, "Accept-Language": "ru,en;q=0.8"}
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        trust_env=False,
        headers=headers,
    ) as client:
        pages = await asyncio.gather(*[_fetch_html(client, url) for url in urls])

    for url, html in zip(urls, pages):
        if not html:
            continue
        matched = matching_offer(spec_item, extract_offers(html, url))
        if matched is None:
            continue
        code, offer = matched
        offer = dict(offer)
        offer["url"] = offer.get("url") or url
        _remember(spec_item, code, offer)
        answer = _answer_from_offer(spec_item, code, offer, "HTTP")
        log = (
            f"\n=== HTTP | позиция {spec_item.id} | {url} ===\n"
            f"артикул {code}, цена {offer['price']}\n{answer}\n"
        )
        return answer, log
    return None
