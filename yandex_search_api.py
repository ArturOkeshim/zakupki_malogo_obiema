"""Поиск ссылок через Yandex Search API v2. Цену отсюда не берём — только URL для браузера."""

from __future__ import annotations

import base64
import os
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

import httpx

WEB_SEARCH_URL = "https://searchapi.api.cloud.yandex.net/v2/web/search"
API_KEY_ENV = "YANDEX_SEARCH_API_KEY"
FOLDER_ENV = "YANDEX_FOLDER_ID"


def is_configured() -> bool:
    return bool(os.environ.get(API_KEY_ENV, "").strip() and os.environ.get(FOLDER_ENV, "").strip())


def _text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


_HEAVY_SHOPS = (
    "ozon.ru",
    "wildberries.ru",
    "wildberries.by",
    "wb.ru",
    "ozon.by",
)
# Часто закрываются SmartCaptcha. Не выкидываем, но ставим после обычных магазинов.
_CAPTCHA_PRONE_SHOPS = (
    "vseinstrumenti.ru",
    "artem-tools.ru",
)
_TENDER_HOST_MARKERS = (
    "zakupki.",
    "kontur.ru",
    "rostender",
    "tenderguru",
    "tender.pro",
    "tenderplan",
    "tenderland",
    "rts-tender",
    "sberbank-ast",
    "sberast",
    "roseltorg",
    "fabrikant.ru",
    "otc.ru",
    "synapse",
    "lineexpo",
    "b2b-center",
    "goszakaz",
    "tektorg",
    "lot-online",
    "astgoz",
    "eis.gosuslugi",
    "zakupki.gov",
    "avito.ru",
    "youla.ru",
    "farpost.ru",
    "supl.biz",
    "promindex.ru",
    "productcenter.ru",
    "clients.site",
    "2gis.ru",
    "uslugi.yandex",
    "profi.ru",
    "smetnoe.ru",
    "garant.ru",
    "docs.cntd.ru",
    "cntd.ru",
    "poisktenderov",
)
_CLASSIFIED_PATH_MARKERS = (
    "yandex.ru/maps",
    "maps.yandex",
    "uslugi.yandex.ru",
)
_DOWNLOAD_SUFFIXES = (
    ".pdf",
    ".xlsx",
    ".xls",
    ".docx",
    ".doc",
    ".zip",
    ".csv",
    ".rtf",
)


def host_of(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _host_of(url: str) -> str:
    return host_of(url)


def is_download_url(url: str) -> bool:
    parsed = urlparse(url)
    path = (parsed.path or "").lower()
    if any(path.endswith(suffix) for suffix in _DOWNLOAD_SUFFIXES):
        return True
    lowered = url.lower()
    return "filestorage" in lowered and "download" in lowered


def is_useless_price_url(url: str) -> bool:
    """Тендеры, файлы и каталоги юрлиц — не карточка с рыночной ценой."""
    if not url:
        return True
    lowered = url.lower()
    if is_download_url(lowered):
        return True
    if any(marker in lowered for marker in _CLASSIFIED_PATH_MARKERS):
        return True
    host = host_of(url)
    return any(marker in host or marker in lowered for marker in _TENDER_HOST_MARKERS)


def _score_url(url: str, kind: str = "retail", has_size: bool = True) -> int:
    host_path = url.lower()
    if "showcaptcha" in host_path:
        return 90
    if any(shop in host_path for shop in _CAPTCHA_PRONE_SHOPS):
        return 8
    heavy = 5 if any(shop in host_path for shop in _HEAVY_SHOPS) else 0
    if kind == "service":
        if "market.yandex.ru" in host_path:
            return 40
        if not has_size and any(
            token in host_path for token in ("calc", "калькулятор", "calculator")
        ):
            return 10 + heavy
        if any(
            token in host_path
            for token in ("price", "tarif", "prajs", "прайс", "stoimost", "calc", "калькулятор")
        ):
            return 0 + heavy
        return 2 + heavy
    if "market.yandex.ru/product" in host_path or "market.yandex.ru/card" in host_path:
        return 0 + heavy
    if "market.yandex.ru" in host_path:
        return 1 + heavy
    return 2 + heavy


def _unique_hosts(results: list[dict]) -> list[dict]:
    seen: set[str] = set()
    unique: list[dict] = []
    for item in results:
        host = _host_of(item.get("url") or "")
        if not host or host in seen:
            continue
        seen.add(host)
        unique.append(item)
    return unique


def parse_search_xml(
    xml_text: str,
    limit: int = 8,
    kind: str = "retail",
    has_size: bool = True,
) -> list[dict]:
    root = ET.fromstring(xml_text)
    results: list[dict] = []
    seen: set[str] = set()
    for doc in root.iter("doc"):
        url = _text(doc.find("url"))
        if not url.startswith("http") or url in seen:
            continue
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            continue
        if is_useless_price_url(url):
            continue
        seen.add(url)
        passages = [_text(passage) for passage in doc.findall(".//passage")]
        results.append(
            {
                "title": _text(doc.find("title")),
                "url": url,
                "snippet": " ".join(part for part in passages if part)[:400],
            }
        )
    results.sort(key=lambda item: _score_url(item["url"], kind=kind, has_size=has_size))
    return results[:limit]


def search_product_links(
    query: str,
    limit: int = 8,
    kind: str = "retail",
    avoid_market: bool = False,
    has_size: bool = True,
) -> list[dict]:
    """Синхронный web/search. Пустой список — нет ключей или пустой ответ."""
    api_key = os.environ.get(API_KEY_ENV, "").strip()
    folder_id = os.environ.get(FOLDER_ENV, "").strip()
    if not api_key or not folder_id:
        return []

    query_text = " ".join(str(query or "").split())[:400]
    body = {
        "query": {
            "searchType": "SEARCH_TYPE_RU",
            "queryText": query_text,
            "familyMode": "FAMILY_MODE_NONE",
            "fixTypoMode": "FIX_TYPO_MODE_ON",
        },
        "folderId": folder_id,
        "groupSpec": {"groupsOnPage": 20},
        "l10n": "LOCALIZATION_RU",
        "region": "225",
        "responseFormat": "FORMAT_XML",
    }
    headers = {
        "Authorization": f"Api-Key {api_key}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=30.0, trust_env=False) as client:
        response = client.post(WEB_SEARCH_URL, headers=headers, json=body)
        response.raise_for_status()
        payload = response.json()

    raw_data = payload.get("rawData")
    if not raw_data:
        return []
    xml_text = base64.b64decode(raw_data).decode("utf-8")
    pool = 30 if avoid_market else max(limit * 3, 12)
    results = parse_search_xml(xml_text, limit=pool, kind=kind, has_size=has_size)
    if avoid_market:
        results = [
            item
            for item in results
            if "market.yandex.ru" not in item["url"].lower()
        ]
    results = [item for item in results if not is_useless_price_url(item.get("url") or "")]
    return _unique_hosts(results)[:limit]


def probe_search_api(query: str = "бумага туалетная focus купить") -> dict:
    """Проверка, что Search API отвечает. В лог — только число ссылок и хосты, без ключа."""
    if not is_configured():
        return {"ok": False, "count": 0, "hosts": [], "error": "нет ключа или folder id"}
    try:
        links = search_product_links(query, kind="retail", avoid_market=True)
    except Exception as error:
        return {"ok": False, "count": 0, "hosts": [], "error": str(error)[:300]}
    hosts: list[str] = []
    for item in links:
        host = urlparse(item["url"]).netloc
        if host and host not in hosts:
            hosts.append(host)
    return {
        "ok": bool(links),
        "count": len(links),
        "hosts": hosts,
        "error": None if links else "пустой ответ",
    }
