"""
Минимальная проверка агента: мост, один агент, короткий ответ.

Без Playwright, без поиска цен, без базы. Если этот скрипт падает,
проблема в ключе, прокси или мосте, а не в пайплайне.

Запуск:
    python test_agent_smoke.py
"""

import asyncio
import time

from cursor_sdk import AgentOptions, LocalAgentOptions

from ask_cursor_agent import (
    ENV_PATH,
    MODEL_ID,
    PROJECT_ROOT,
    async_agent_bridge,
    load_env_file,
)

PROMPT = (
    "Не открывай сайты и не вызывай инструменты. "
    "Ответь одним словом: pong"
)


def log(started: float, message: str) -> None:
    elapsed = time.monotonic() - started
    print(f"[{elapsed:6.1f} с] {message}", flush=True)


async def run_smoke() -> None:
    started = time.monotonic()
    load_env_file(ENV_PATH)
    log(started, "Ключ и прокси из .env загружены.")
    log(started, f"Модель: {MODEL_ID}")
    log(started, "Поднимаю мост...")

    async with async_agent_bridge() as (client, api_key):
        log(started, "Мост готов. Создаю агента без Playwright...")
        options = AgentOptions(
            api_key=api_key,
            model=MODEL_ID,
            local=LocalAgentOptions(
                cwd=PROJECT_ROOT,
                setting_sources=["user"],
            ),
        )
        async with await client.agents.create(options) as agent:
            log(started, f"Агент создан, id={agent.agent_id}")
            log(started, f"Отправляю запрос: {PROMPT}")
            run = await agent.send(PROMPT)
            log(started, "Запрос ушёл. Жду ответ...")
            async for message in run.messages():
                note = getattr(message, "message", "") or ""
                kind = getattr(message, "type", "")
                if kind == "status" and note:
                    log(started, f"статус: {note}")
                elif kind == "assistant":
                    text = getattr(message, "text", None) or getattr(message, "result", "") or ""
                    if text:
                        log(started, f"черновик: {text}")
            result = await run.wait()
            log(started, f"Прогон закончился со статусом {result.status}")
            print()
            print("--- ответ агента ---")
            print(result.result or "(пусто)")
            print(f"id агента: {agent.agent_id}")
            print(f"id прогона: {result.id}")
            print(f"длительность: {result.duration_ms} мс")


def main() -> None:
    try:
        asyncio.run(run_smoke())
    except KeyboardInterrupt:
        raise SystemExit("Остановлено вручную.")


if __name__ == "__main__":
    main()
