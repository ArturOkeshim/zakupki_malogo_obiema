"""Диагностика: что Яндекс показывает браузеру на вкладке «Товары»."""
import urllib.parse
from patchright.sync_api import sync_playwright

QUERY = "Счётчик холодной воды Decast ОСВХ-25 «НЕПТУН» цена"
encoded_query = urllib.parse.quote(QUERY)
URL = (
    "https://ya.ru/search"
    f"?text={encoded_query}"
    "&products_mode=1"
    "&from=tabbar"
    "&serp-reload-from=tab"
    "&lr=213"
)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context(
        locale="ru-RU",
        viewport={"width": 1280, "height": 900},
    )
    page = context.new_page()

    page.goto(URL, timeout=60_000, wait_until="domcontentloaded")
    page.wait_for_timeout(10_000)

    print("URL:", page.url)
    print("Title:", page.title())

    selectors = [
        "text=Подтвердите, что вы не робот",
        "text=Вы не робот",
        '[class*="captcha"]',
        '[class*="SmartCaptcha"]',
        ".serp-item",
        "[data-fast-name]",
        "[data-zone-name]",
        "[data-auto]",
        "a[href*='market.yandex']",
        "a[href*='pokupki.market']",
        "[class*='Product']",
        "[class*='product']",
        "[class*='Snippet']",
        "[class*='snippet']",
        "[class*='Promo']",
        "[class*='promo']",
        "[class*='Card']",
    ]
    for sel in selectors:
        loc = page.locator(sel)
        print(f"  {sel!r}: count={loc.count()}")

    # Какие data-атрибуты реально есть на странице — по ним потом ищем карточки.
    debug_info = page.evaluate(
        """() => {
            const unique = (items) => [...new Set(items)].slice(0, 80);
            const zoneNames = unique(
                [...document.querySelectorAll('[data-zone-name]')]
                    .map((el) => el.getAttribute('data-zone-name'))
            );
            const fastNames = unique(
                [...document.querySelectorAll('[data-fast-name]')]
                    .map((el) => el.getAttribute('data-fast-name'))
            );
            const autoNames = unique(
                [...document.querySelectorAll('[data-auto]')]
                    .map((el) => el.getAttribute('data-auto'))
            );
            const classTokens = unique(
                [...document.querySelectorAll('[class]')]
                    .flatMap((el) => String(el.className).split(/\\s+/))
                    .filter((name) => /product|snippet|promo|card|price|market/i.test(name))
            );
            const priceLike = [...document.querySelectorAll('body *')]
                .filter((el) => {
                    const text = el.childNodes.length
                        ? [...el.childNodes]
                            .filter((node) => node.nodeType === 3)
                            .map((node) => node.textContent)
                            .join('')
                        : '';
                    return text.includes('₽') || /\\d[\\s\\u00a0]*₽/.test(el.textContent || '');
                })
                .slice(0, 20)
                .map((el) => ({
                    tag: el.tagName,
                    className: String(el.className).slice(0, 120),
                    text: (el.innerText || '').replace(/\\s+/g, ' ').slice(0, 120),
                }));
            return { zoneNames, fastNames, autoNames, classTokens, priceLike };
        }"""
    )
    # Пишем в файл: консоль Windows ломается на символе ₽.
    import json
    with open("ya_debug.json", "w", encoding="utf-8") as f:
        json.dump(debug_info, f, ensure_ascii=False, indent=2)

    page.screenshot(path="ya_debug.png", full_page=True)
    html = page.content()
    with open("ya_debug.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("Saved ya_debug.png, ya_debug.html, ya_debug.json")

    page.wait_for_timeout(3000)
    browser.close()
