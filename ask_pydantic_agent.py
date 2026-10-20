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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx2
import logfire
from dotenv import load_dotenv
from pydantic_ai import Agent
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.settings import ModelSettings
from pydantic_ai.toolsets import WrapperToolset
from pydantic_ai_harness.playwright import PlaywrightBrowser

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
_agent: Agent[PriceSearchDeps, str] | None = None
_runtime_ready = False
_openrouter_proxy = ""

INSTRUCTIONS = (
    "Ты ищешь рыночную цену в рублях. Инструментов web search и web fetch нет. "
    "Цену бери только из snapshot карточки, которую открыл сам. "
    "Инструменты: navigate, snapshot, click, wait_for. "
    "Первый navigate — поле yandex_search_url из INPUT, если оно есть: это Яндекс Маркет. "
    "Дальше карточку открывай только click по ссылке из snapshot, адрес карточки сам не составляй. "
    "Не больше двух navigate: второй — только если выдача Маркета пустая, не из-за капчи. "
    "Не открывай Google, Bing, DuckDuckGo, Ozon и Wildberries: у них antibot, цена оттуда не снимется. "
    "Капча или «Вы не робот»: не уходи на другой сайт. "
    "Кликни чекбокс «Я не робот», если он есть. "
    "Потом wait_for(gone=True) по тексту капчи с timeout_ms=60000 и снова snapshot. "
    "Окно видно человеку — он может пройти проверку, подожди. "
    "Если за минуту капча не снялась — not_found, без второго navigate. "
    "Через execute_js и fetch цену не доставай. "
    "Не изменяй файлы проекта. Верни только один JSON-объект по схеме из запроса."
)


@dataclass
class PriceSearchDeps:
    """Счётчик переходов живёт в deps, чтобы параллельные прогоны его не делили."""

    page_limit: int = 5
    opened_pages: int = 0
    log_title: str = ""


@dataclass
class PageLimitToolset(WrapperToolset[PriceSearchDeps]):
    """После PAGE_LIMIT вызовов navigate новые адреса не открываются. click не лимитируем."""

    page_limit: int = 5

    async def call_tool(self, name, tool_args, ctx, tool):
        deps = ctx.deps
        if name in PAGE_TOOLS:
            if deps.opened_pages >= deps.page_limit:
                return (
                    f"Лимит страниц {deps.page_limit} исчерпан. "
                    "Новые страницы не открывай. "
                    "Верни итоговый JSON по уже открытым страницам."
                )
            deps.opened_pages += 1
            print(
                f"  {deps.log_title}: переход {deps.opened_pages}/{deps.page_limit} "
                f"{tool_args.get('url', '')}"
            )
        elif name == "click":
            print(f"  {deps.log_title}: клик")
        return await self.wrapped.call_tool(name, tool_args, ctx, tool)


@dataclass
class LimitedPlaywrightBrowser(PlaywrightBrowser[PriceSearchDeps]):
    page_limit: int = 5

    async def for_run(self, ctx):
        """Новый Chromium на каждый прогон. replace() у PlaywrightBrowser копирует сессию и трое делят одно окно."""
        return LimitedPlaywrightBrowser(
            headless=self.headless,
            allowed_domains=list(self.allowed_domains) if self.allowed_domains else None,
            block_private_addresses=self.block_private_addresses,
            screenshot_on_navigate=self.screenshot_on_navigate,
            max_content_tokens=self.max_content_tokens,
            action_timeout_ms=self.action_timeout_ms,
            navigation_timeout_ms=self.navigation_timeout_ms,
            chromium_sandbox=self.chromium_sandbox,
            auto_install_chromium=self.auto_install_chromium,
            storage_state=_load_storage_state(),
            cdp_url=self.cdp_url,
            page_limit=self.page_limit,
        )

    async def wrap_run(self, ctx, *, handler):
        self._session.tracer = ctx.tracer
        async with self._session:
            try:
                return await handler()
            finally:
                await _save_storage_state(self._session)

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


def _load_storage_state():
    if not STORAGE_STATE_PATH.is_file():
        return None
    try:
        return json.loads(STORAGE_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


async def _save_storage_state(session) -> None:
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
    async with _storage_lock:
        STORAGE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = STORAGE_STATE_PATH.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(STORAGE_STATE_PATH)


def _openrouter_model_name() -> str:
    if MODEL_ID.startswith("openrouter:"):
        return MODEL_ID.split(":", 1)[1]
    return MODEL_ID


def _make_openrouter_http_client(proxy: str):
    timeout = httpx2.Timeout(600.0, connect=5.0)
    if proxy:
        return httpx2.AsyncClient(proxy=proxy, timeout=timeout, trust_env=False)
    return httpx2.AsyncClient(timeout=timeout, trust_env=False)


def prepare_runtime() -> None:
    """OpenRouter из России не открывается — прокси только у http-клиента модели.

    HTTPS_PROXY из .env в окружение процесса не кладём: Playwright его подхватит,
    и поиск в браузере пойдёт через тот же выход, что API. Маркету это VPN.
    """
    global _runtime_ready, _openrouter_proxy
    load_dotenv(ENV_PATH)
    proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or "").strip()
    if proxy and "://" not in proxy:
        proxy = "http://" + proxy
    _openrouter_proxy = proxy
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(key, None)
    _apply_no_proxy_for_logfire()
    if proxy:
        print(f"Модель OpenRouter через прокси: {proxy.split('@')[-1]}")
        print("Поиск в браузере напрямую, без прокси")
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
    global _agent, _price_log_lock, _storage_lock
    _agent = None
    _price_log_lock = None
    _storage_lock = None


def get_agent() -> Agent[PriceSearchDeps, str]:
    global _agent
    if _agent is None:
        print(f"Создаю агента: {MODEL_ID}")
        provider = OpenRouterProvider(
            http_client=_make_openrouter_http_client(_openrouter_proxy),
        )
        _agent = Agent(
            OpenRouterModel(_openrouter_model_name(), provider=provider),
            output_type=str,
            deps_type=PriceSearchDeps,
            instructions=INSTRUCTIONS,
            model_settings=ModelSettings(parallel_tool_calls=False),
            capabilities=[
                LimitedPlaywrightBrowser(headless=False, action_timeout_ms=30_000)
            ],
        )
    return _agent


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
) -> tuple[str, str]:
    """Один товар — один прогон агента. Возвращает (текст ответа, ход прогона)."""
    if not _runtime_ready:
        prepare_runtime()
        configure_observability()

    deps = PriceSearchDeps(page_limit=page_limit, log_title=log_title)
    print(f"Поднимаю агента pydantic: {log_title}")
    result = await get_agent().run(prompt, deps=deps)
    answer = (result.output or "").strip()
    agent_log = _log_from_messages(log_title, result.all_messages(), answer)
    await _append_log_block(agent_log)
    print(f"Агент закончил: {log_title}")
    return answer, agent_log
