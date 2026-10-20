"""
Один товар, один агент. Ответ печатается в консоль, ход работы — в файл лога.
В базу ничего не пишется.

Тот же лимит страниц и тот же лог, что у пайплайна. Отличается только текст запроса
и файл, куда пишется лог.

Запуск:
    python test_search_price_script.py "Гель для стирки Mr.Green Шерсть и шелк 5л"

Лог: data/test_search_log.txt
"""

import asyncio
import sys
from pathlib import Path

from ask_cursor_agent import (
    ENV_PATH,
    async_agent_bridge,
    ask_on_async_bridge,
    load_env_file,
)
from run_pipeline import PAGE_LIMIT

LOG_PATH = Path(__file__).resolve().parent / "data" / "test_search_log.txt"


def build_prompt(product_name: str) -> str:
    """Короткий текст запроса. Чем он меньше, тем меньше токенов уходит на инструкцию."""
    return (
        "Найди цену в рублях на один товар. Работай быстро.\n"
        "Через playwright открой одну карточку магазина "
        "(Яндекс Маркет, Ozon или Wildberries) и прочитай цену со страницы.\n"
        "Хватит одного сайта. Если точной карточки нет, цену не выдумывай.\n"
        "Не изменяй файлы проекта.\n"
        "Верни только один JSON-объект, без markdown и без текста вокруг:\n"
        '{"name": "...", "price_rub": 123.45, "url": "https://...", "note": "что открыл"}\n'
        "Если цену на странице не увидел, поставь price_rub равным null.\n\n"
        f"Товар: {product_name}"
    )


async def search_one_product(product_name: str) -> None:
    """Мост — связь с Cursor. Агент внутри него один."""
    prompt = build_prompt(product_name)
    print("Поднимаю мост и одного агента.")
    print("Товар:", product_name)
    async with async_agent_bridge() as (client, api_key):
        answer, _agent_log = await ask_on_async_bridge(
            client,
            api_key,
            prompt,
            log_path=LOG_PATH,
            log_title=product_name,
            page_limit=PAGE_LIMIT,
        )
    print("\n--- ответ агента ---")
    print(answer)
    print(f"\nЛог: {LOG_PATH}")


def main() -> None:
    # sys.argv[0] — имя этого файла. Текст после него в команде — это argv[1].
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        raise SystemExit(
            'Укажи товар в кавычках:\n'
            '  python test_search_price_script.py "Гель для стирки Mr.Green 5л"'
        )

    product_name = sys.argv[1].strip()
    load_env_file(ENV_PATH)
    asyncio.run(search_one_product(product_name))


if __name__ == "__main__":
    main()
