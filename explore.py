"""Manual exploration entry point.

Run this to open alensa.cz in a visible browser, accept cookies, and pause so
you can inspect selectors with the Playwright inspector / devtools.

    python explore.py             # opens, takes screenshot, closes
    python explore.py --pause     # opens and stays open until you press Enter
"""
from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright

from src.sites import alensa_cz


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pause", action="store_true",
                        help="Keep the browser open until you press Enter")
    parser.add_argument("--headless", action="store_true",
                        help="Run without showing the browser window")
    args = parser.parse_args()

    screenshot = Path("reports") / "homepage.png"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        context = browser.new_context(
            locale="cs-CZ",
            viewport={"width": 1366, "height": 900},
        )
        page = context.new_page()

        safe = alensa_cz.open_homepage(page, screenshot_path=screenshot)
        print(f"Opened: {page.url}")
        print(f"Title:  {page.title()}")
        print(f"Screenshot: {screenshot.resolve()}")

        if args.pause:
            input("Press Enter to close the browser...")

        context.close()
        browser.close()


if __name__ == "__main__":
    main()
