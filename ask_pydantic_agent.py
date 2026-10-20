"""
Поиск рыночной цены через pydantic-ai и Playwright.

Мост Cursor здесь не используется. Модель — OpenRouter, браузер — окно Chromium.
Жёсткий лимит переходов: navigate и click сверх PAGE_LIMIT не открывают новую страницу.

Запуск отдельно не нужен: этот модуль вызывает run_pipeline_pydantic.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from yandex_search_api import is_download_url, is_useless_price_url

import httpx2
import logfire
from dotenv import load_dotenv
from pydantic_ai import Agent
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.settings import ModelSettings
from pydantic_ai.toolsets import WrapperToolset
from pydantic_ai_harness.playwright import PlaywrightBrowser

from price_research import parse_response_text

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
ENV_PATH = PROJECT_ROOT / ".env"
PRICE_SEARCH_LOG_PATH = DATA_DIR / "price_search_log_pydantic.txt"
STORAGE_STATE_PATH = DATA_DIR / "browser_storage_state.json"
MODEL_IDS = {
    "haiku": "openrouter:anthropic/claude-haiku-5.5",
    # OpenRouter :batch — отдельный Batch API, не chat/completions. Agent.run его не принимает (404).
    "haiku_batch": "openrouter:anthropic/claude-haiku-5.5:batch",
}
MODEL_NAME = "haiku"
MODEL_ID = MODEL_IDS[MODEL_NAME]
# Как в Cursor: лимит только на navigate. Карточку товара открывают click, его не считаем.
PAGE_TOOLS = frozenset({"navigate"})

_price_log_lock: asyncio.Lock | None = None
_storage_lock: asyncio.Lock | None = None
_launch_lock: asyncio.Lock | None = None
_agents: dict[int, Agent[PriceSearchDeps, str]] = {}
_worker_browsers: dict[int, LimitedPlaywrightBrowser] = {}
_session_entered: dict[int, bool] = {}
_runtime_ready = False
_openrouter_proxies: list[str] = []
_market_blocked_this_run = False
_captcha_hosts_this_run: set[str] = set()
_dead_hosts_by_item: dict[int, set[str]] = {}
_opened_urls_by_item: dict[int, set[str]] = {}
_ITEM_ID_IN_TITLE = re.compile(r"позиция\s+(\d+)", re.IGNORECASE)
WAIT_FOR_MAX_MS = 8_000
NAVIGATE_TIMEOUT_MS = 8_000
_BLOCKED_MARKERS = (
    "showcaptcha",
    "smartcaptcha",
    "доступ запрещ",
    "http/1.1 403",
    "status code: 403",
    "status: 403",
    "ошибка 403",
    "я не робот",
    "вы не робот",
    "are you not a robot",
    "/showcaptcha",
    "яндекс 403",
    "title: 403",
    "проверка пользователя",
)
_DEAD_HOST_MARKERS = (
    "err_connection",
    "err_timed_out",
    "err_name_not_resolved",
    "err_ssl",
    "err_empty_response",
    "err_aborted",
    "net::err_",
    "timeout ",
    "timed out",
    "нет соединения",
    "ns_error_net",
    "ddos-guard",
    "403 error",
    "is forbidden",
)

INSTRUCTIONS = (
    "Ты снимаешь цену с карточки в браузере. Не изображай поисковик: "
    "не составляй google/yandex URL. Инструмента execute_js нет. "
    "search_plan=market_then_api: сначала yandex_search_url (Маркет). "
    "Если 403/капча — сразу первая yandex_api_links, Маркет больше не открывай. Не сдавайся с captcha, пока не прошёл список. "
    "search_plan=api_first: сразу первая yandex_api_links, Маркет не открывай. "
    "search_plan=market_only: только yandex_search_url. "
    "По списку ссылок иди по порядку. Сайт, который не открылся или отдал капчу, больше не открывай. "
    "Капча не тратит лимит страниц. "
    "Каталог магазина — click или navigate на карточку ТОГО ЖЕ сайта, хост не бросай. "
    "Avito, карты, объявления — не цена. Тендеры и файлы — сразу следующая ссылка магазина. "
    "color_is_flexible=true: серый/белый/бежевый на мешке не отказ, если марка и фасовка те. "
    "manufacturer_is_legal_name=true: не требуй это ООО на карточке, сверяй товар/ГОСТ/объём. "
    "Артикул в name/model совпал, а одна характеристика ему противоречит — бери цену карточки, расхождение в note. "
    "tender_price_per_unit уже за одну unit. «В упаковке N» не делает её ценой пачки. market_price тоже за unit. "
    "has_size=false у услуги: калькуляторы без тарифа пропускай. "
    "Минимальный тариф той же услуги — confidence=low. Связь/интернет/ЖКХ без города и числа линий не бери. "
    "Цена в title открытой карточки — можно брать, сразу JSON. "
    "Нашёл цену — инструменты больше не вызывай. Не изменяй файлы. Верни только один JSON-объект."
)


def is_market_blocked_this_run() -> bool:
    return _market_blocked_this_run


def mark_market_blocked_this_run() -> None:
    global _market_blocked_this_run
    if _market_blocked_this_run:
        return
    _market_blocked_this_run = True
    print("Маркет недоступен в этом прогоне: следующие позиции сразу по ссылкам API")


def captcha_hosts_this_run() -> set[str]:
    return set(_captcha_hosts_this_run)


def mark_captcha_host(host: str) -> None:
    if not host or host in _captcha_hosts_this_run or "market.yandex.ru" in host:
        return
    _captcha_hosts_this_run.add(host)
    print(f"Капча на {host}: в этом прогоне сайт больше не открываем, страница в лимит не идёт")


def reset_run_search_flags() -> None:
    """Только в начале прогона, не между позициями и не между заходами одного прогона."""
    global _market_blocked_this_run
    _market_blocked_this_run = False
    _captcha_hosts_this_run.clear()
    _dead_hosts_by_item.clear()
    _opened_urls_by_item.clear()


def dead_hosts_for_item(item_id: int) -> set[str]:
    return set(_dead_hosts_by_item.get(item_id) or [])


def opened_urls_for_item(item_id: int) -> set[str]:
    return set(_opened_urls_by_item.get(item_id) or [])


def _item_id_from_title(log_title: str) -> int | None:
    match = _ITEM_ID_IN_TITLE.search(log_title or "")
    if not match:
        return None
    return int(match.group(1))


def _norm_page_url(url: str) -> str:
    return str(url or "").split("#", 1)[0].rstrip("/").lower()


def _remember_dead_host(log_title: str, host: str) -> None:
    item_id = _item_id_from_title(log_title)
    if not item_id or not host:
        return
    _dead_hosts_by_item.setdefault(item_id, set()).add(host)


def _remember_opened_url(log_title: str, url: str) -> None:
    item_id = _item_id_from_title(log_title)
    if not item_id or not url:
        return
    _opened_urls_by_item.setdefault(item_id, set()).add(_norm_page_url(url))


def _looks_like_yandex_403(result) -> bool:
    text = str(result or "").lower()
    head = text[:1500]
    if "яндекс 403" in head or "title: 403" in head:
        return True
    return "403" in head and ("яндекс" in head or "yandex" in head)


def _page_host(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _looks_like_dead_host(result) -> bool:
    text = str(result or "").lower()
    return any(marker in text for marker in _DEAD_HOST_MARKERS)


@dataclass
class PriceSearchDeps:
    """Счётчик переходов живёт в deps, чтобы параллельные прогоны его не делили."""

    page_limit: int = 5
    opened_pages: int = 0
    log_title: str = ""
    captcha_seen: bool = False
    last_navigate_counted: bool = False
    last_host: str = ""
    dead_hosts: set[str] = field(default_factory=set)
    seen_urls: set[str] = field(default_factory=set)


@dataclass
class PageLimitToolset(WrapperToolset[PriceSearchDeps]):
    """После PAGE_LIMIT вызовов navigate новые адреса не открываются. click не лимитируем."""

    page_limit: int = 5

    async def get_tools(self, ctx):
        tools = await super().get_tools(ctx)
        return {name: tool for name, tool in tools.items() if name != "execute_js"}

    async def call_tool(self, name, tool_args, ctx, tool):
        deps = ctx.deps
        args = dict(tool_args or {})
        url = str(args.get("url") or "")
        host = _page_host(url)
        page_url = _norm_page_url(url)
        if name == "execute_js":
            return (
                "execute_js нет. "
                "Если цена уже есть в title или snapshot — сразу верни JSON, без инструментов."
            )
        if name in PAGE_TOOLS:
            on_market = "market.yandex.ru" in url.lower()
            if not on_market:
                deps.captcha_seen = False
            elif deps.captcha_seen or is_market_blocked_this_run():
                return (
                    "Маркет уже отдал капчу/403. Эту ссылку не открывай. "
                    "Сразу следующая yandex_api_links — любой другой сайт. "
                    "Не ставь fail_reason=captcha, пока список ссылок не кончился."
                )
            if host and host in captcha_hosts_this_run():
                print(f"  {deps.log_title}: пропуск {host}, капча уже была в прогоне")
                return (
                    f"Сайт {host} уже отдал капчу в этом прогоне. Не открывай его. "
                    "Следующая ссылка другого хоста."
                )
            if is_useless_price_url(url) or is_download_url(url):
                _remember_opened_url(deps.log_title, url)
                print(f"  {deps.log_title}: пропуск тендера/файла {url}")
                return (
                    "Это тендер, объявление, карта или скачивание файла, не карточка с ценой. "
                    "Сразу следующая ссылка магазина из yandex_api_links."
                )
            if host and host in deps.dead_hosts:
                print(f"  {deps.log_title}: пропуск {host}, сайт уже не открылся")
                return (
                    f"Сайт {host} уже не открылся. Не открывай его снова. "
                    "Следующая ссылка другого хоста из yandex_api_links."
                )
            if page_url and page_url in deps.seen_urls:
                return (
                    "Эту страницу уже открывал. "
                    "Открой другую карточку на этом сайте или другой хост."
                )
            if deps.opened_pages >= deps.page_limit:
                return (
                    f"Лимит страниц {deps.page_limit} исчерпан. "
                    "Новые страницы не открывай. "
                    "Верни итоговый JSON по уже открытым страницам."
                )
            deps.last_host = host
        elif name == "click":
            print(f"  {deps.log_title}: клик")
        elif name == "wait_for":
            if deps.captcha_seen:
                return (
                    "Капча/403 на этой странице. wait_for не вызывай. "
                    "Открой следующую ссылку — любой сайт, кроме Маркета."
                )
            timeout = int(args.get("timeout_ms") or WAIT_FOR_MAX_MS)
            if timeout > WAIT_FOR_MAX_MS:
                args["timeout_ms"] = WAIT_FOR_MAX_MS
                print(f"  {deps.log_title}: wait_for урезан до {WAIT_FOR_MAX_MS} мс")
        try:
            result = await self.wrapped.call_tool(name, args, ctx, tool)
        except Exception as error:
            if name in PAGE_TOOLS:
                deps.opened_pages += 1
                deps.last_navigate_counted = True
                if host:
                    deps.dead_hosts.add(host)
                    _remember_dead_host(deps.log_title, host)
                print(
                    f"  {deps.log_title}: переход {deps.opened_pages}/{deps.page_limit} "
                    f"не открылся {url}"
                )
                return (
                    f"Страница не открылась ({error}). "
                    "Этот сайт больше не открывай. Следующая ссылка другого хоста."
                )
            raise
        if name in PAGE_TOOLS and _looks_like_dead_host(result):
            deps.opened_pages += 1
            deps.last_navigate_counted = True
            if page_url:
                deps.seen_urls.add(page_url)
                _remember_opened_url(deps.log_title, url)
            if host:
                deps.dead_hosts.add(host)
                _remember_dead_host(deps.log_title, host)
            print(
                f"  {deps.log_title}: переход {deps.opened_pages}/{deps.page_limit} "
                f"сайт не открылся {url}"
            )
            extra = (
                f"\nСайт {host or 'не открылся'}. Не открывай его снова. "
                "Следующая ссылка другого хоста из yandex_api_links."
            )
            if isinstance(result, str):
                return result + extra
            return f"{result}{extra}"
        on_market = "market.yandex.ru" in url.lower()
        blocked = _looks_like_blocked(result) or (on_market and _looks_like_yandex_403(result))
        if on_market and ("403" in str(result or "").lower()[:1500] or blocked):
            mark_market_blocked_this_run()
        if name in {"navigate", "snapshot", "get_text"} and blocked:
            deps.captcha_seen = True
            if on_market or _looks_like_yandex_403(result):
                mark_market_blocked_this_run()
            elif host or deps.last_host:
                mark_captcha_host(host or deps.last_host)
            if name in PAGE_TOOLS:
                if page_url:
                    deps.seen_urls.add(page_url)
                    _remember_opened_url(deps.log_title, url)
                deps.last_navigate_counted = False
                print(f"  {deps.log_title}: капча, в лимит не входит {url}")
            elif deps.last_navigate_counted:
                deps.opened_pages = max(0, deps.opened_pages - 1)
                deps.last_navigate_counted = False
                print(f"  {deps.log_title}: капча на открытой странице, слот возвращён")
            extra = (
                "\nЭто капча или 403. В лимит страниц не входит. Не вызывай wait_for. "
                "Этот сайт в прогоне больше не открывай. Следующая ссылка другого хоста. "
                "Не ставь captcha, пока в списке ещё есть ссылки."
            )
            if isinstance(result, str):
                return result + extra
            return f"{result}{extra}"
        if name in PAGE_TOOLS:
            deps.opened_pages += 1
            deps.last_navigate_counted = True
            if page_url:
                deps.seen_urls.add(page_url)
                _remember_opened_url(deps.log_title, url)
            print(
                f"  {deps.log_title}: переход {deps.opened_pages}/{deps.page_limit} {url}"
            )
        return result


@dataclass
class LimitedPlaywrightBrowser(PlaywrightBrowser[PriceSearchDeps]):
    page_limit: int = 5
    worker_number: int = 1

    def __post_init__(self) -> None:
        super().__post_init__()
        self._toolset._id = self.id or f"playwright-w{self.worker_number}"

    async def for_run(self, ctx):
        """Тот же объект: Chromium живёт на агенте, а не на каждой позиции."""
        return self

    async def wrap_run(self, ctx, *, handler):
        session = self._session
        session.tracer = ctx.tracer
        if not _session_entered.get(self.worker_number):
            original_connect = session._connect

            async def connect_one_browser(playwright):
                async with _get_launch_lock():
                    print(f"  Агент {self.worker_number}: Chromium запущен, дальше тот же")
                    return await original_connect(playwright)

            session._connect = connect_one_browser
            await session.__aenter__()
            _session_entered[self.worker_number] = True
        else:
            await _reset_browser_tabs(session)
            print(f"  Агент {self.worker_number}: тот же Chromium")
        return await handler()

    def get_toolset(self):
        return PageLimitToolset(wrapped=self._toolset, page_limit=self.page_limit)


LOGFIRE_DIRECT_HOSTS = (
    "logfire-us.pydantic.dev",
    "logfire-eu.pydantic.dev",
    "logfire.pydantic.dev",
    "127.0.0.1",
    "localhost",
)


def _apply_no_proxy_for_logfire() -> None:
    """Прокси нужен OpenRouter, а не Logfire: через него API Logfire не отвечает за 10 с."""
    items = [
        item.strip()
        for item in os.environ.get("NO_PROXY", "").split(",")
        if item.strip()
    ]
    for host in LOGFIRE_DIRECT_HOSTS:
        if host not in items:
            items.append(host)
    value = ",".join(items)
    os.environ["NO_PROXY"] = value
    os.environ["no_proxy"] = value


def _storage_path(worker_number: int) -> Path:
    return DATA_DIR / f"browser_storage_state_w{worker_number}.json"


def _load_storage_state(worker_number: int = 1):
    for path in (_storage_path(worker_number), STORAGE_STATE_PATH):
        if not path.is_file():
            continue
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
    return None


def _get_launch_lock() -> asyncio.Lock:
    global _launch_lock
    if _launch_lock is None:
        _launch_lock = asyncio.Lock()
    return _launch_lock


def _looks_like_blocked(result) -> bool:
    text = str(result or "").lower()
    return any(marker in text for marker in _BLOCKED_MARKERS)


async def _reset_browser_tabs(session) -> None:
    pages = list(getattr(session, "pages", None) or [])
    page = getattr(session, "page", None)
    extras = [item for item in pages if item is not page]
    for extra in extras:
        try:
            await extra.close()
        except Exception:
            continue
    if page is None:
        return
    try:
        await page.goto("about:blank", timeout=5_000)
    except Exception:
        return


def _make_browser(worker_number: int, page_limit: int = 5) -> LimitedPlaywrightBrowser:
    return LimitedPlaywrightBrowser(
        headless=True,
        action_timeout_ms=NAVIGATE_TIMEOUT_MS,
        max_content_tokens=12_000,
        page_limit=page_limit,
        worker_number=worker_number,
        storage_state=_load_storage_state(worker_number),
        id=f"playwright-w{worker_number}",
    )


def get_worker_browser(worker_number: int, page_limit: int = 5) -> LimitedPlaywrightBrowser:
    browser = _worker_browsers.get(worker_number)
    if browser is None:
        browser = _make_browser(worker_number, page_limit)
        _worker_browsers[worker_number] = browser
        return browser
    browser.page_limit = page_limit
    return browser


async def close_worker_browsers() -> None:
    """Закрыть Chromium до конца asyncio-цикла."""
    for number, browser in list(_worker_browsers.items()):
        try:
            await _save_storage_state(browser._session, number)
        except Exception:
            pass
        try:
            await browser._session.__aexit__(None, None, None)
        except Exception:
            pass
    _worker_browsers.clear()
    _session_entered.clear()


async def _save_storage_state(session, worker_number: int = 1) -> None:
    context = getattr(session, "_context", None)
    if context is None:
        return
    try:
        state = await context.storage_state()
    except Exception:
        return
    global _storage_lock
    if _storage_lock is None:
        _storage_lock = asyncio.Lock()
    path = _storage_path(worker_number)
    async with _storage_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(path)


def _openrouter_model_name() -> str:
    if MODEL_ID.startswith("openrouter:"):
        return MODEL_ID.split(":", 1)[1]
    return MODEL_ID


def _normalize_proxy(raw: str) -> str:
    proxy = raw.strip()
    if not proxy:
        return ""
    if "://" not in proxy:
        proxy = "http://" + proxy
    return proxy


def _load_openrouter_proxies() -> list[str]:
    """Выходы для модели. Браузер их не видит.

    Первый HTTPS_PROXY отключён: через него OpenRouter рвал соединение.
    Берём HTTPS_PROXY_2 и дальше, либо OPENROUTER_PROXIES.
    """
    packed = os.environ.get("OPENROUTER_PROXIES", "").strip()
    found: list[str] = []
    if packed:
        for part in packed.split(","):
            proxy = _normalize_proxy(part)
            if proxy:
                found.append(proxy)
        return found
    for index in range(2, 10):
        extra = _normalize_proxy(os.environ.get(f"HTTPS_PROXY_{index}", ""))
        if extra:
            found.append(extra)
    if found:
        return found
    fallback = _normalize_proxy(
        os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or ""
    )
    if fallback:
        print("HTTPS_PROXY_2 нет, временно берётся HTTPS_PROXY")
        found.append(fallback)
    return found


def proxy_for_worker(worker_number: int) -> str:
    if not _openrouter_proxies:
        return ""
    return _openrouter_proxies[(worker_number - 1) % len(_openrouter_proxies)]


def _make_openrouter_http_client(proxy: str):
    # Жилой прокси часто не успевает за 5 с: OpenRouter тогда падает с Connection error.
    timeout = httpx2.Timeout(25.0, connect=10.0, read=20.0, write=15.0, pool=10.0)
    if proxy:
        return httpx2.AsyncClient(proxy=proxy, timeout=timeout, trust_env=False)
    return httpx2.AsyncClient(timeout=timeout, trust_env=False)


def prepare_runtime() -> None:
    """OpenRouter из России не открывается — прокси только у http-клиента модели.

    HTTPS_PROXY из .env в окружение процесса не кладём: Playwright его подхватит,
    и поиск в браузере пойдёт через тот же выход, что API. Маркету это VPN.
    """
    global _runtime_ready, _openrouter_proxies
    load_dotenv(ENV_PATH)
    _openrouter_proxies = _load_openrouter_proxies()
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(key, None)
    _apply_no_proxy_for_logfire()
    if _openrouter_proxies:
        exits = [proxy.split("@")[-1] for proxy in _openrouter_proxies]
        print(f"Прокси OpenRouter: {len(exits)} шт. ({', '.join(exits)})")
        print("Первый HTTPS_PROXY отключён, все агенты через HTTPS_PROXY_2")
        print("Поиск в браузере напрямую, без прокси")
    yandex_key = os.environ.get("YANDEX_SEARCH_API_KEY", "").strip()
    yandex_folder = os.environ.get("YANDEX_FOLDER_ID", "").strip()
    if yandex_key and yandex_folder:
        print(
            "Yandex Search API: ключ есть. Сразу — услуги и товар без Маркета; "
            "остальным — если Маркет не откроется"
        )
    else:
        print(
            "Yandex Search API: нет YANDEX_SEARCH_API_KEY или YANDEX_FOLDER_ID в .env — "
            "агент идёт на Маркет/поиск без готовых ссылок"
        )
    if not os.environ.get("OPENROUTER_API_KEY", "").strip():
        raise SystemExit(
            "Нет OPENROUTER_API_KEY.\n"
            "Добавьте ключ в .env и снова запустите: python run_pipeline_pydantic.py"
        )
    print(f"Модель: {MODEL_NAME} ({MODEL_ID})")
    _runtime_ready = True


def configure_observability() -> None:
    logfire.configure(send_to_logfire="if-token-present")
    logfire.instrument_pydantic_ai()


def reset_agent() -> None:
    """Новый цикл asyncio не может пользоваться клиентом и замком предыдущего asyncio.run."""
    global _agents, _price_log_lock, _storage_lock, _launch_lock
    _agents = {}
    _price_log_lock = None
    _storage_lock = None
    _launch_lock = None


def get_agent(worker_number: int = 1) -> Agent[PriceSearchDeps, str]:
    agent = _agents.get(worker_number)
    if agent is None:
        proxy = proxy_for_worker(worker_number)
        exit_host = proxy.split("@")[-1] if proxy else "напрямую"
        print(f"Создаю агента {worker_number}: {MODEL_ID}, OpenRouter через {exit_host}")
        provider = OpenRouterProvider(
            http_client=_make_openrouter_http_client(proxy),
        )
        agent = Agent(
            OpenRouterModel(_openrouter_model_name(), provider=provider),
            output_type=str,
            deps_type=PriceSearchDeps,
            instructions=INSTRUCTIONS,
            model_settings=ModelSettings(parallel_tool_calls=False),
        )
        _agents[worker_number] = agent
    return agent


def _clip_text(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… обрезано, всего {len(text)} символов"


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _log_from_messages(log_title: str, messages, answer: str) -> str:
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    parts = [f"\n=== {started_at} | {log_title} ===\n"]
    for message in messages:
        for part in getattr(message, "parts", []):
            part_kind = getattr(part, "part_kind", "") or type(part).__name__
            if part_kind in {"tool-call", "ToolCallPart"}:
                parts.append(
                    f"вызов {getattr(part, 'tool_name', '')}: {_as_text(getattr(part, 'args', ''))}\n"
                )
            elif part_kind in {"tool-return", "ToolReturnPart"}:
                seen = _clip_text(_as_text(getattr(part, "content", "")).strip())
                if seen:
                    parts.append(f"увидено ({getattr(part, 'tool_name', '')}):\n{seen}\n")
            elif part_kind in {"text", "TextPart"}:
                text = str(getattr(part, "content", "") or "").strip()
                if text:
                    parts.append(f"текст: {text}\n")
    parts.append("итог:\n")
    parts.append(answer + "\n")
    return "".join(parts)


async def _append_log_block(text: str) -> None:
    global _price_log_lock
    if _price_log_lock is None:
        _price_log_lock = asyncio.Lock()
    PRICE_SEARCH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with _price_log_lock:
        with PRICE_SEARCH_LOG_PATH.open("a", encoding="utf-8") as log_file:
            log_file.write(text)


async def ask_price(
    prompt: str,
    log_title: str,
    page_limit: int = 5,
    worker_number: int = 1,
    skip_hosts: set[str] | None = None,
    skip_urls: set[str] | None = None,
) -> tuple[str, str]:
    """Один товар — один прогон агента. Возвращает (текст ответа, ход прогона)."""
    if not _runtime_ready:
        prepare_runtime()
        configure_observability()

    deps = PriceSearchDeps(
        page_limit=page_limit,
        log_title=log_title,
        dead_hosts=set(skip_hosts or ()),
        seen_urls=set(skip_urls or ()),
    )
    browser = get_worker_browser(worker_number, page_limit=page_limit)
    print(f"Поднимаю агента pydantic {worker_number}: {log_title}")
    agent = get_agent(worker_number)
    result = await agent.run(
        prompt,
        deps=deps,
        capabilities=[browser],
    )
    answer = (result.output or "").strip()
    try:
        parse_response_text(answer)
    except json.JSONDecodeError:
        print(f"  {log_title}: ответ не JSON, прошу переписать без браузера")
        result = await agent.run(
            "Предыдущий ответ не является валидным JSON. "
            "Не открывай сайты и не вызывай инструменты. "
            "Верни только один JSON-объект по схеме из первого сообщения, "
            "без markdown и без текста до и после. "
            "Кавычки внутри строк экранируй, все скобки закрой.",
            message_history=result.all_messages(),
            deps=deps,
        )
        answer = (result.output or "").strip()
    agent_log = _log_from_messages(log_title, result.all_messages(), answer)
    await _append_log_block(agent_log)
    print(f"Агент закончил: {log_title}")
    return answer, agent_log
