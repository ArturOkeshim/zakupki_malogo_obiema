"""Диагностика: что WB показывает браузеру (patchright)."""
import urllib.parse
from patchright.sync_api import sync_playwright

QUERY = "кроссовки женские"
URL = "https://www.wildberries.ru/catalog/0/search.aspx?search=" + urllib.parse.quote(QUERY)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context(
        locale="ru-RU",
        viewport={"width": 1280, "height": 900},
    )
    page = context.new_page()

    page.goto(URL, timeout=60_000, wait_until="domcontentloaded")
    page.wait_for_timeout(8000)

    print("URL:", page.url)
    print("Title:", page.title())

    # Что видно на странице
    selectors = [
        "div.product-card-list",
        'a[data-testid="product-card-link"]',
        ".preloader",
        ".loader",
        '[class*="loader"]',
        '[class*="captcha"]',
        '[class*="antibot"]',
        "#captcha",
        "text=Подтвердите, что вы не робот",
        "text=Доступ ограничен",
    ]
    for sel in selectors:
        loc = page.locator(sel)
        print(f"  {sel!r}: count={loc.count()}")

    # Сохраним скрин и HTML для разбора
    page.screenshot(path="wb_debug.png", full_page=True)
    html = page.content()
    with open("wb_debug.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("Saved wb_debug.png, wb_debug.html")

    # localStorage — WB хранит тут anti-bot токены
    pow_token = page.evaluate("() => localStorage.getItem('session-pow-token')")
    print("session-pow-token:", pow_token[:80] if pow_token else None)

    page.wait_for_timeout(5000)
    browser.close()
