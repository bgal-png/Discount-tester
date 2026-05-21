"""Stateful probe: open product, add to cart, dump cart & checkout structure.

Writes reports/probe_<step>.{png,html,txt} for each step.
Run headed (default) so we can watch what happens.

    python probe_flow.py            # visible browser
    python probe_flow.py --headless
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from playwright.sync_api import sync_playwright, Page

from src.sites import alensa_cz
from probe import dump_buttons, dump_inputs, dump_headings, dump_prices


PRODUCT_URL = "https://www.alensa.cz/gelone-monthly-3-cocky"
CART_URL = "https://www.alensa.cz/nakup/kosik/"
CHECKOUT_URL = "https://www.alensa.cz/nakup/doprava-a-platba/"


def dump_page(page: Page, name: str) -> None:
    reports = Path("reports")
    reports.mkdir(exist_ok=True)
    page.screenshot(path=str(reports / f"probe_{name}.png"), full_page=True)
    (reports / f"probe_{name}.html").write_text(page.content(), encoding="utf-8")
    summary = [
        f"URL: {page.url}",
        f"TITLE: {page.title()}",
        "",
        *dump_headings(page),
        "",
        *dump_buttons(page),
        "",
        *dump_inputs(page),
        "",
        *dump_prices(page),
    ]
    (reports / f"probe_{name}.txt").write_text("\n".join(summary), encoding="utf-8")
    print(f"  wrote probe_{name}.{{png,html,txt}}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless, slow_mo=200)
        context = browser.new_context(locale="cs-CZ",
                                      viewport={"width": 1366, "height": 900})
        page = context.new_page()

        print("[1/4] Homepage + cookies")
        alensa_cz.open_homepage(page)

        print("[2/4] Product page")
        page.goto(PRODUCT_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        dump_page(page, "product")

        # Try clicking the visible "Vložit do košíku" link. Contact lenses need
        # required variant selects to be filled first — if the click doesn't
        # add anything, we'll know and pick a simpler product next.
        print("[3/4] Attempt add-to-cart")
        add = page.locator("a.detail-addToBasket-button").first
        if add.count() > 0 and add.is_visible():
            try:
                add.click()
                page.wait_for_timeout(2500)
                print(f"  clicked add-to-cart; current URL: {page.url}")
            except Exception as e:
                print(f"  add-to-cart click failed: {e}")
        else:
            print("  add-to-cart link not visible (cookie banner? variants required?)")

        print("[4/4] Cart + checkout pages")
        page.goto(CART_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        dump_page(page, "cart")

        proceed = page.locator("a.btn-proceed").first
        if proceed.count() > 0 and proceed.is_visible():
            proceed.click()
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(2000)
            print(f"  proceeded to: {page.url}")
        else:
            print("  proceed button not visible; staying on cart")
        dump_page(page, "checkout")

        context.close()
        browser.close()


if __name__ == "__main__":
    main()
