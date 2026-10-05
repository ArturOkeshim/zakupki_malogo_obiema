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

import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from cursor_sdk import (
    AgentOptions,
    CursorAgentError,
    CursorClient,
    DefaultHttpxClient,
    LocalAgentOptions,
)
import cursor_sdk._bridge as bridge_module
from cursor_sdk.errors import CursorSDKError

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
BATCH_TXT_PATH = DATA_DIR / "batch.txt"
RESPONSE_JSON_PATH = DATA_DIR / "response.json"
ENV_PATH = PROJECT_ROOT / ".env"
PROXY_PRELOAD_PATH = PROJECT_ROOT / "workaround" / "proxy_preload.cjs"

# ЗАМЕТКА ДЛЯ ОБУЧЕНИЯ: модель — это «какой мозг» у агента.
# composer-2.5 — обычный выбор для таких скриптов.
MODEL_ID = "composer-2.5"


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


def make_agent_options(api_key: str) -> AgentOptions:
    return AgentOptions(
        api_key=api_key,
        model=MODEL_ID,
        # ЗАМЕТКА ДЛЯ ОБУЧЕНИЯ:
        # webSearch — встроенный поиск со сниппетами, без открытия сайта.
        # Его нет в списке инструментов, поэтому агент не может им «отделаться».
        disallowed_tools=["webSearch"],
        local=LocalAgentOptions(
            cwd=PROJECT_ROOT,
            # "project" читает .cursor/mcp.json в этой папке (там Playwright).
            # "user" читает ~/.cursor/mcp.json, если он есть.
            setting_sources=["user", "project"],
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
    options = make_agent_options(api_key)
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
            "Сначала соберите батч: его пишет run_pipeline.py"
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
