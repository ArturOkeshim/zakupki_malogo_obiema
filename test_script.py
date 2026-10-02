from patchright.sync_api import sync_playwright
import time

with sync_playwright() as p:    browser = p.chromium.launch(headless=False)
    page = browser.new_page()
    page.goto('https://www.wildberries.ru/catalog/0/search.aspx?search=%D0%BA%D1%80%D0%BE%D1%81%D1%81%D0%BE%D0%B2%D0%BA%D0%B8+%D0%B6%D0%B5%D0%BD%D1%81%D0%BA%D0%B8%D0%B5')
    time.sleep(60)
    browser.close()