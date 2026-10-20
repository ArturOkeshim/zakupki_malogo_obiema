"""
Отправляет текст батча локальному агенту Cursor и сохраняет его ответ.

Как запускать:
    python ask_cursor_agent.py smoke
    python ask_cursor_agent.py run

Перед запуском в файле .env должна быть строка:
    CURSOR_API_KEY=ключ с https://cursor.com/dashboard/integrations

Исходящий прокси моста (не обязателен). Пример:
    HTTPS_PROXY=http://127.0.0.1:10809
Если строки нет, скрипт берёт системный прокси Windows.
Локальный адрес 127.0.0.1 в прокси не отправляется.
"""

import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import httpx
from cursor_sdk import (
    AgentOptions,
    AsyncClient,
    CursorAgentError,
    CursorClient,
    DefaultHttpxClient,
    LocalAgentOptions,
    StdioMcpServerConfig,
)
import cursor_sdk._bridge as bridge_module
from cursor_sdk.errors import CursorSDKError

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
BATCH_TXT_PATH = DATA_DIR / "batch.txt"
RESPONSE_JSON_PATH = DATA_DIR / "response.json"
ENV_PATH = PROJECT_ROOT / ".env"
PROXY_PRELOAD_PATH = PROJECT_ROOT / "workaround" / "proxy_preload.cjs"
# Ход поиска цен из пайплайна. Тестовый скрипт пишет в свой файл.
PRICE_SEARCH_LOG_PATH = DATA_DIR / "price_search_log.txt"
# Три агента пишут в один файл. Замок собирает строки одной позиции в один блок.
_price_log_lock: asyncio.Lock | None = None

# ЗАМЕТКА ДЛЯ ОБУЧЕНИЯ: модель — это «какой мозг» у агента.
# composer-2.5 — обычный выбор для таких скриптов.
MODEL_ID = "claude-haiku-5-5"


class AgentStopped(Exception):
    """Прогон агента оборвался. Этот работник должен остановиться, товар тут ни при чём."""


def load_env_file(path: Path) -> None:
    """Кладёт пары KEY=value из .env в окружение, если их ещё нет."""
    if not path.is_file():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def extract_json_text(raw: str) -> str:
    """Убирает обёртку ```json, если агент всё же добавил её."""
    text = raw.strip()
    if not text.startswith("```"):
        return text

    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def read_bridge_discovery_on_windows(process, timeout: float):
    """
    Ждёт строку «мост готов» из stderr.

    ЗАМЕТКА ДЛЯ ОБУЧЕНИЯ:
    На Windows встроенный код SDK ждёт эту строку через select().
    select() умеет сокеты, а stderr процесса — это канал (pipe), поэтому
    Windows отвечает ошибкой 10038. readline() канал читает нормально.
    """
    if process.stderr is None:
        raise CursorSDKError("Bridge process stderr is unavailable")

    deadline = time.monotonic() + timeout
    stderr_lines: list[str] = []
    while time.monotonic() < deadline:
        line = process.stderr.readline()
        if line == "":
            raise CursorSDKError(
                "Bridge exited before discovery with status "
                f"{process.poll()}: {''.join(stderr_lines)}"
            )
        stderr_lines.append(line)
        discovery = bridge_module.parse_discovery_line(line)
        if discovery is not None:
            return discovery

    raise CursorSDKError(
        "Timed out waiting for bridge discovery\n" + "".join(stderr_lines)
    )


def install_windows_bridge_fix() -> None:
    if sys.platform != "win32":
        return
    bridge_module._read_discovery = read_bridge_discovery_on_windows


def windows_system_proxy_url() -> str:
    """Адрес прокси из настроек Windows, без логина и пароля в ответе."""
    if sys.platform != "win32":
        return ""
    import winreg

    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        )
        enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
        server, _ = winreg.QueryValueEx(key, "ProxyServer")
    except OSError:
        return ""
    if not enabled or not server:
        return ""
    server = str(server).strip()
    if "@" in server:
        server = server.split("@", 1)[1]
    if "://" not in server:
        server = "http://" + server
    return server


def install_node_proxy_preload() -> None:
    """
    ЗАМЕТКА ДЛЯ ОБУЧЕНИЯ:
    Мост — процесс Node. Он копирует окружение при старте.
    NODE_OPTIONS=--require файл.cjs запускает наш код раньше кода моста.
    proxy_preload.cjs подменяет http/https/http2, чтобы запросы к Cursor
    шли через HTTPS_PROXY. Встроенный Node так сам не делает.
    """
    if not PROXY_PRELOAD_PATH.is_file():
        raise SystemExit(f"Нет файла {PROXY_PRELOAD_PATH}")

    preload_path = str(PROXY_PRELOAD_PATH).replace("\\", "/")
    require_flag = "--require " + preload_path
    current = os.environ.get("NODE_OPTIONS", "")
    if preload_path in current:
        return
    if current:
        os.environ["NODE_OPTIONS"] = current + " " + require_flag
    else:
        os.environ["NODE_OPTIONS"] = require_flag
    print(f"NODE_OPTIONS: {os.environ['NODE_OPTIONS']}")


def apply_outbound_proxy() -> None:
    """
    Прокси только для выхода моста в интернет.

    ЗАМЕТКА ДЛЯ ОБУЧЕНИЯ:
    Мост — отдельная программа. Она копирует переменные окружения в момент запуска.
    HTTPS_PROXY говорит ей: «сайты Cursor открывай через прокси».
    NO_PROXY говорит: «адрес 127.0.0.1 не проксируй» — это сам мост на этом компьютере.
    Python к мосту и так ходит напрямую (trust_env=False) и эти переменные не использует.
    """
    proxy = os.environ.get("HTTPS_PROXY", "").strip()
    if not proxy:
        proxy = windows_system_proxy_url()
    if not proxy:
        print("Исходящий прокси не задан. Мост пойдёт к Cursor напрямую.")
        return
    if "://" not in proxy:
        proxy = "http://" + proxy

    os.environ["HTTPS_PROXY"] = proxy
    os.environ["HTTP_PROXY"] = proxy
    # Встроенный Node моста читает HTTP(S)_PROXY только с этим флагом.
    os.environ["NODE_USE_ENV_PROXY"] = "1"

    no_proxy_items = [
        item.strip()
        for item in os.environ.get("NO_PROXY", "").split(",")
        if item.strip()
    ]
    for item in ("127.0.0.1", "localhost"):
        if item not in no_proxy_items:
            no_proxy_items.append(item)
    os.environ["NO_PROXY"] = ",".join(no_proxy_items)

    shown = proxy.split("@")[-1]
    print(f"Исходящий прокси моста: {shown}")
    print(f"Без прокси: {os.environ['NO_PROXY']}")


def prepare_agent_runtime() -> str:
    """Настраивает прокси и возвращает ключ. Вызывать до запуска моста."""
    install_windows_bridge_fix()
    apply_outbound_proxy()
    install_node_proxy_preload()
    api_key = os.environ.get("CURSOR_API_KEY", "").strip()
    if not api_key:
        raise SystemExit(
            "Нет CURSOR_API_KEY.\n"
            "1. Откройте https://cursor.com/dashboard/integrations\n"
            "2. Создайте User API key\n"
            "3. Добавьте в .env строку CURSOR_API_KEY=...\n"
            "4. Снова запустите: python ask_cursor_agent.py smoke"
        )
    return api_key


def playwright_command() -> str:
    # На Windows команда npx — это npx.cmd. CreateProcess без .cmd её не находит.
    if sys.platform == "win32":
        return "npx.cmd"
    return "npx"


def make_agent_options(api_key: str, browser_profile_dir: str) -> AgentOptions:
    return AgentOptions(
        api_key=api_key,
        model=MODEL_ID,
        # ЗАМЕТКА ДЛЯ ОБУЧЕНИЯ:
        # webSearch — встроенный поиск со сниппетами, без открытия сайта.
        # Его нет в списке инструментов, поэтому агент не может им «отделаться».
        disallowed_tools=["webSearch"],
        # ЗАМЕТКА ДЛЯ ОБУЧЕНИЯ:
        # .cursor/mcp.json поднимает один Playwright на всех. У него один профиль
        # на проект, поэтому второе окно не открывается.
        # Свой сервер на каждого агента и свой каталог профиля — это отдельный браузер.
        mcp_servers={
            "playwright": StdioMcpServerConfig(
                command=playwright_command(),
                # ЗАМЕТКА ДЛЯ ОБУЧЕНИЯ:
                # @latest заставляет npx при каждом запуске спрашивать npm, нет ли новой версии.
                # Точная версия берётся из кэша npx без похода в интернет.
                # Обновлять версию — вручную, поменяв число здесь.
                args=[
                    "-y",
                    "@playwright/mcp@0.0.83",
                    # Без окна: браузер работает в фоне. Капча на Яндексе и Ozon от этого бывает чаще.
                    "--headless",
                    "--user-data-dir=" + browser_profile_dir,
                ],
            ),
        },
        local=LocalAgentOptions(
            cwd=PROJECT_ROOT,
            # "user" — серверы из ~/.cursor/mcp.json, если они есть.
            # "project" не берём: оттуда снова подключился бы общий Playwright.
            setting_sources=["user"],
        ),
    )


@contextmanager
def agent_bridge():
    """
    Один мост на несколько товаров.

    ЗАМЕТКА ДЛЯ ОБУЧЕНИЯ:
    Мост — отдельная программа, через неё Python говорит с Cursor.
    Агент — один разговор внутри моста. Новый агент не помнит предыдущий товар.
    Мост при этом заново не запускаем.
    """
    api_key = prepare_agent_runtime()
    try:
        with CursorClient.launch_bridge(
            workspace=PROJECT_ROOT,
            http_client=DefaultHttpxClient(trust_env=False),
        ) as client:
            yield client, api_key
    except CursorAgentError as error:
        raise SystemExit(
            f"Агент не стартовал: {error.message}\n"
            f"Можно повторить: {error.is_retryable}"
        ) from error


def collect_answer(run) -> str:
    """Дожидается конца прогона и возвращает текст ответа. Пустой текст тоже возвращает."""
    status_notes: list[str] = []
    for message in run.messages():
        if getattr(message, "type", "") != "status":
            continue
        note = getattr(message, "message", "") or ""
        if note:
            status_notes.append(note)
    result = run.wait()

    if result.status != "finished":
        details = "\n".join(status_notes)
        raise SystemExit(
            f"Агент закончил со статусом {result.status}, id={result.id}\n{details}"
        )
    return result.result or ""


def answer_is_json(raw: str) -> bool:
    try:
        json.loads(extract_json_text(raw))
        return True
    except json.JSONDecodeError:
        return False


def ask_on_bridge(client, api_key: str, prompt: str, retry_if_not_json: bool = False) -> str:
    """
    Новый агент в уже открытом мосте. После ответа агент закрывается.

    ЗАМЕТКА ДЛЯ ОБУЧЕНИЯ:
    Второй agent.send идёт в тот же разговор. Агент помнит поиск
    и заново сайты не открывает — его просят только переписать ответ в JSON.
    """
    # Каталог живёт, пока открыт этот агент. У другого агента каталог другой.
    with tempfile.TemporaryDirectory(prefix="playwright-agent-", ignore_cleanup_errors=True) as browser_profile_dir:
        options = make_agent_options(api_key, browser_profile_dir)
        with client.agents.create(options) as agent:
            answer = collect_answer(agent.send(prompt))
            if not retry_if_not_json:
                if not answer.strip():
                    raise SystemExit("Пустой ответ агента.")
                return answer

            if answer_is_json(answer):
                return answer

            print("Ответ не JSON. Прошу тот же агент вернуть только JSON, без нового поиска.")
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            (DATA_DIR / "response_raw.txt").write_text(answer, encoding="utf-8")
            retry_prompt = (
                "Предыдущий ответ не является JSON. Не открывай сайты заново. "
                "Верни только один JSON-объект по схеме из первого сообщения, "
                "без markdown и без текста до и после."
            )
            return collect_answer(agent.send(retry_prompt))


@asynccontextmanager
async def async_agent_bridge():
    """
    Один мост для нескольких агентов сразу.

    ЗАМЕТКА ДЛЯ ОБУЧЕНИЯ:
    Обычный CursorClient синхронный: пока один агент ищет, второй не стартует.
    AsyncClient умеет держать несколько агентов на одном мосте.
    Смешивать эти два клиента в одном прогоне нельзя.
    """
    api_key = prepare_agent_runtime()
    # trust_env=False: Python ходит к мосту на 127.0.0.1 напрямую, мимо HTTPS_PROXY.
    # ЗАМЕТКА ДЛЯ ОБУЧЕНИЯ:
    # timeout здесь — сколько httpx ждёт байты от моста на одном чтении.
    # Агент с Playwright часто молчит дольше минуты (капча, долгая страница).
    # При timeout=60 это выглядит как: Bridge request timed out: ReadTimeout.
    # connect — только установка связи; read — ожидание ответа во время работы агента.
    http_client = httpx.AsyncClient(
        trust_env=False,
        follow_redirects=True,
        timeout=httpx.Timeout(connect=30.0, read=600.0, write=60.0, pool=60.0),
    )
    try:
        try:
            client = await AsyncClient.launch_bridge(
                workspace=PROJECT_ROOT,
                http_client=http_client,
            )
        except CursorAgentError as error:
            raise SystemExit(
                f"Агент не стартовал: {error.message}\n"
                f"Можно повторить: {error.is_retryable}"
            ) from error
        async with client:
            yield client, api_key
    finally:
        await http_client.aclose()


def _as_text(value) -> str:
    """Аргументы и ответы инструментов приходят словарём. В лог нужен читаемый текст."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _opens_page(message) -> bool:
    """Переход на новый адрес. Часто он называется mcp, а browser_navigate лежит в аргументах."""
    if message.name == "browser_navigate":
        return True
    return "browser_navigate" in _as_text(message.args)


def _clip_text(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… обрезано, всего {len(text)} символов"


def _usage_line(usage) -> str:
    if usage is None:
        return "токены: нет данных"
    return (
        "токены: "
        f"input={usage.input_tokens}, "
        f"output={usage.output_tokens}, "
        f"cache_read={usage.cache_read_tokens}, "
        f"cache_write={usage.cache_write_tokens}, "
        f"reasoning={usage.reasoning_tokens}, "
        f"total={usage.total_tokens}"
    )


def _log_message_line(message) -> str:
    message_type = getattr(message, "type", "")

    if message_type == "tool_call" and message.status == "running":
        return f"вызов {message.name}: {_as_text(message.args)}"

    if message_type == "tool_call" and message.status == "completed":
        seen = _clip_text(_as_text(message.result).strip())
        if not seen:
            return ""
        return f"увидено ({message.name}):\n{seen}"

    if message_type == "thinking":
        text = (message.text or "").strip()
        if not text:
            return ""
        return f"размышление: {text}"

    if message_type == "usage":
        return _usage_line(message.usage)

    if message_type == "status" and message.message:
        return f"статус: {message.message}"

    return ""


async def _watch_run(run, lines: list[str], page_limit: int):
    """
    Собирает шаги прогона. page_limit — сколько адресов можно открыть.
    0 значит: любой новый адрес сразу обрывает прогон.
    """
    opened_call_ids: set[str] = set()
    hit_limit = False

    async for message in run.messages():
        line = _log_message_line(message)
        if line:
            lines.append(line)

        if getattr(message, "type", "") != "tool_call" or message.status != "running":
            continue
        if not _opens_page(message):
            continue
        if message.call_id in opened_call_ids:
            continue

        opened_call_ids.add(message.call_id)
        if len(opened_call_ids) <= page_limit:
            continue

        # Сообщение приходит, когда переход уже начался. cancel его обрывает.
        lines.append(f"стоп: лимит страниц {page_limit}")
        hit_limit = True
        if run.supports("cancel"):
            await run.cancel()
        break

    result = await run.wait()
    return result, hit_limit


async def _append_log_block(log_path: Path, text: str) -> None:
    """Один блок на позицию. Пока пишем его, второй агент ждёт, строки не перемешиваются."""
    global _price_log_lock
    if _price_log_lock is None:
        _price_log_lock = asyncio.Lock()

    log_path.parent.mkdir(parents=True, exist_ok=True)
    async with _price_log_lock:
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(text)


@asynccontextmanager
async def _one_agent_at_a_time(lock: asyncio.Lock | None):
    """Очередь только на запуск. Сам поиск идёт параллельно, замок его не держит."""
    if lock is None:
        yield
        return
    async with lock:
        yield


async def ask_on_async_bridge(
    client,
    api_key: str,
    prompt: str,
    log_path: Path,
    log_title: str,
    page_limit: int = 3,
    start_lock: asyncio.Lock | None = None,
) -> tuple[str, str]:
    """
    Новый агент в уже открытом мосте. После ответа агент закрывается.

    Возвращает (JSON-ответ, ход прогона). Ход — размышления, вызовы браузера и итог.
    Больше page_limit адресов агент не откроет. Если лимит сработал или ответ
    не JSON, тот же агент ещё раз пишет JSON и сайты заново не открывает.

    start_lock — общая очередь запуска. Пока один агент создаётся и не получил
    первый ответ моста, следующий ждёт. Дальше они ищут одновременно.
    """
    lines: list[str] = []
    # Каталог живёт, пока открыт этот агент. У другого агента каталог другой.
    with tempfile.TemporaryDirectory(prefix="playwright-agent-", ignore_cleanup_errors=True) as browser_profile_dir:
        options = make_agent_options(api_key, browser_profile_dir)
        agent_context = None
        # ЗАМЕТКА ДЛЯ ОБУЧЕНИЯ:
        # CreateAgent — короткий запрос с пределом 60 секунд.
        # Три таких запроса разом мост не успевает, и SDK обрывает их по таймауту.
        # Замок отпускаем после send: к этому моменту прогон уже начался,
        # а следующий агент только тогда встаёт на создание.
        if start_lock is not None and start_lock.locked():
            print(f"Жду очередь на запуск: {log_title}")
        async with _one_agent_at_a_time(start_lock):
            print(f"Поднимаю агента: {log_title}")
            agent_context = await client.agents.create(options)
            agent = await agent_context.__aenter__()
            try:
                run = await agent.send(prompt)
            except BaseException:
                await agent_context.__aexit__(*sys.exc_info())
                agent_context = None
                raise
            print(f"Агент запущен: {log_title}")

        try:
            result, hit_limit = await _watch_run(run, lines, page_limit)
            need_json_only = hit_limit or not answer_is_json(result.result or "")
            agent_broke = result.status != "finished" and not hit_limit
            if agent_broke:
                lines.append(f"прогон закончился со статусом {result.status}")
                await _append_log_block(log_path, _log_block(log_title, lines, result))
                raise AgentStopped(
                    f"Агент закончил со статусом {result.status}, id={result.id}"
                )

            if need_json_only:
                lines.append("Прошу JSON по уже открытым страницам, без нового поиска.")
                retry_prompt = (
                    "Новые страницы не открывай. "
                    "Верни только один JSON-объект по схеме из первого сообщения, "
                    "без markdown и без текста до и после."
                )
                retry_run = await agent.send(retry_prompt)
                result, _hit_limit = await _watch_run(retry_run, lines, page_limit=0)
                if result.status != "finished":
                    lines.append(f"прогон закончился со статусом {result.status}")
                    await _append_log_block(log_path, _log_block(log_title, lines, result))
                    raise AgentStopped(
                        f"Агент закончил со статусом {result.status}, id={result.id}"
                    )
        finally:
            if agent_context is not None:
                await agent_context.__aexit__(None, None, None)

    agent_log = _log_block(log_title, lines, result)
    await _append_log_block(log_path, agent_log)
    return result.result or "", agent_log


def _log_block(log_title: str, lines: list[str], result) -> str:
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    parts = [f"\n=== {started_at} | {log_title} ===\n"]
    for line in lines:
        parts.append(line + "\n")
    parts.append("итог:\n")
    parts.append((result.result or "") + "\n")
    parts.append(_usage_line(result.usage) + "\n")
    parts.append(f"длительность: {result.duration_ms} мс\n")
    return "".join(parts)


def ask_agent(prompt: str) -> str:
    """Один запрос: мост поднимается только ради этого агента."""
    with agent_bridge() as (client, api_key):
        return ask_on_bridge(client, api_key, prompt)


def cmd_smoke() -> None:
    """Короткий запрос без поиска цен: проверяем, что ключ и SDK живые."""
    prompt = (
        "Ответь одним JSON-объектом без markdown и без пояснений: "
        '{"ok": true, "message": "sdk-smoke"}'
    )
    answer = ask_agent(prompt)
    print(answer)


def cmd_run() -> None:
    """Берёт готовый батч, отдаёт агенту, пишет JSON ответа."""
    if not BATCH_TXT_PATH.is_file():
        raise SystemExit(
            f"Нет файла {BATCH_TXT_PATH}\n"
            "Сначала соберите батч: python price_research.py export"
        )

    prompt = BATCH_TXT_PATH.read_text(encoding="utf-8")
    prompt = (
        prompt
        + "\n\nНе изменяй файлы проекта. Верни только один JSON-объект в ответе."
    )

    print("Отправляю батч агенту. Это может занять несколько минут...")
    answer = ask_agent(prompt)
    json_text = extract_json_text(answer)

    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError as error:
        raw_path = DATA_DIR / "response_yandex_raw.txt"
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(answer, encoding="utf-8")
        raise SystemExit(
            f"Ответ не JSON ({error}). Сырой текст сохранён в {raw_path}"
        ) from error

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RESPONSE_JSON_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Ответ записан: {RESPONSE_JSON_PATH}")


def main() -> None:
    load_env_file(ENV_PATH)

    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "smoke":
        cmd_smoke()
        return
    if command == "run":
        cmd_run()
        return

    raise SystemExit(
        "Укажите команду:\n"
        "  python ask_cursor_agent.py smoke\n"
        "  python ask_cursor_agent.py run"
    )


if __name__ == "__main__":
    main()
