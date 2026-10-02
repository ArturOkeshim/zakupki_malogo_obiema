"""
Самый простой запрос к локальному агенту Cursor.

Прокси для моста — тот же обход, что в workaround/:
preload Node + HTTPS_PROXY, а Python к мосту ходит напрямую.

Запуск (venv):
    python ask_cursor_smoke.py
"""

import os
from pathlib import Path

from cursor_sdk import AgentOptions, CursorClient, DefaultHttpxClient, LocalAgentOptions
from dotenv import load_dotenv

from ask_cursor_agent import (
    apply_outbound_proxy,
    install_node_proxy_preload,
    install_windows_bridge_fix,
    windows_system_proxy_url,
)

PROJECT_ROOT = Path(__file__).resolve().parent


def print_proxy_state() -> None:
    print("HTTPS_PROXY =", os.environ.get("HTTPS_PROXY") or "(нет)")
    print("HTTP_PROXY  =", os.environ.get("HTTP_PROXY") or "(нет)")
    print("NO_PROXY    =", os.environ.get("NO_PROXY") or "(нет)")
    print("NODE_OPTIONS =", os.environ.get("NODE_OPTIONS") or "(нет)")
    print("прокси Windows =", windows_system_proxy_url() or "(выключен или нет)")
    print()


load_dotenv(PROJECT_ROOT / ".env")
install_windows_bridge_fix()
apply_outbound_proxy()
install_node_proxy_preload()
print_proxy_state()

api_key = os.environ.get("CURSOR_API_KEY", "").strip()
if not api_key:
    raise SystemExit("Нет CURSOR_API_KEY в .env")

# ЗАМЕТКА ДЛЯ ОБУЧЕНИЯ:
# trust_env=False — Python говорит с мостом на 127.0.0.1 напрямую.
# В интернет через прокси ходит уже Node, с proxy_preload.cjs.
print("Отправляю запрос агенту...")
with CursorClient.launch_bridge(
    workspace=PROJECT_ROOT,
    http_client=DefaultHttpxClient(trust_env=False),
) as client:
    options = AgentOptions(
        api_key=api_key,
        model="composer-2.5",
        local=LocalAgentOptions(cwd=str(PROJECT_ROOT)),
    )
    with client.agents.create(options) as agent:
        run = agent.send("Ответь одним словом: ок")
        print("run id:", run.id)
        for message in run.messages():
            kind = getattr(message, "type", "")
            note = getattr(message, "message", "") or ""
            print(f"  событие: {kind}" + (f" — {note}" if note else ""))
        result = run.wait()

print("status:", result.status)
print("duration_ms:", result.duration_ms)
print("ответ:", result.result or "(пусто)")
