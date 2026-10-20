import asyncio
import os

import logfire
from pydantic_ai import Agent
from pydantic_ai_harness.playwright import PlaywrightBrowser
from dotenv import load_dotenv

load_dotenv()

# 1) Logfire = OpenTelemetry. В консоли видны spans сразу; на logfire.dev — если есть LOGFIRE_TOKEN.
logfire.configure(send_to_logfire="if-token-present")

# 2) Включает трейсы для всех Agent: run → запросы к модели → вызовы tools.
logfire.instrument_pydantic_ai()

agent = Agent(
    "openrouter:deepseek/deepseek-v4.1-flash",
    output_type=str,
    capabilities=[PlaywrightBrowser(headless=False)],
)


async def main() -> None:
    result = await agent.run(
        """Открой yandex market и найди цену ручку eric crause - сколько они там в среднем стоят?
        Не открывай больше 2-3 страниц"""
    )
    print(result.output)
    logfire.force_flush()  # дождаться отправки spans перед выходом процесса


if __name__ == "__main__":
    asyncio.run(main())
