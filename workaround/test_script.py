import asyncio
import os
import sys

from dotenv import load_dotenv
from cursor_sdk import LocalAgentOptions
from cursor_sdk.asyncio import AsyncAgent, AsyncClient, DefaultAsyncHttpxClient

# ЗАМЕТКА ДЛЯ ОБУЧЕНИЯ: load_dotenv читает файл .env и кладёт строки KEY=VALUE
# в os.environ. Мост потом копирует это окружение, поэтому прокси тоже подхватится.
load_dotenv()

# Мост — отдельный процесс Node. --require запускает proxy_preload.cjs
# до его кода, и тогда http, https и http2 тоже идут через HTTPS_PROXY.
preload_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxy_preload.cjs")
preload_path = preload_path.replace("\\", "/")
require_flag = "--require " + preload_path
current_node_options = os.environ.get("NODE_OPTIONS", "")
if preload_path not in current_node_options:
    if current_node_options:
        os.environ["NODE_OPTIONS"] = current_node_options + " " + require_flag
    else:
        os.environ["NODE_OPTIONS"] = require_flag


async def main() -> None:
    # ЗАМЕТКА ДЛЯ ОБУЧЕНИЯ: async with сам закроет процесс, когда блок закончится.
    # await нужен, потому что запуск моста — это ожидание, а не обычный вызов.
    # trust_env=False: говорить с мостом на 127.0.0.1 напрямую, мимо прокси Windows.
    http_client = DefaultAsyncHttpxClient(trust_env=False)
    async with await AsyncClient.launch_bridge(
        workspace=os.getcwd(),
        http_client=http_client,
    ) as client:
        async with await AsyncAgent.create(
            client=client,
            model="composer-2.5",
            api_key=os.environ["CURSOR_API_KEY"],
            local=LocalAgentOptions(cwd=os.getcwd()),
        ) as agent:
            run = await agent.send("Summarize what this repository does")
            answer_parts = []
            error_text = ""
            async for event in run.messages():
                if event.type == "assistant":
                    content_blocks = event.message.content
                    for block in content_blocks:
                        if block.type == "text" and block.text:
                            answer_parts.append(block.text)
                if event.type == "status" and event.status.upper() == "ERROR":
                    error_text = event.message
            await run.wait()

            answer = "".join(answer_parts)
            if answer:
                print(answer)
            if error_text:
                print(error_text)


def ignore_closed_pipe(unraisable):
    # ЗАМЕТКА ДЛЯ ОБУЧЕНИЯ: это сообщение Python печатает уже после завершения,
    # когда убирает канал процесса. На ответ агента оно не влияет.
    error = unraisable.exc_value
    if isinstance(error, ValueError) and "closed pipe" in str(error):
        return
    sys.__unraisablehook__(unraisable)


sys.unraisablehook = ignore_closed_pipe
asyncio.run(main())
