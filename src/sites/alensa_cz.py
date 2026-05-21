"""alensa.cz site adapter.

For now: opens the homepage, accepts cookies if a banner appears, captures a
screenshot. Selectors for product search / add-to-cart / discount code input
will be added once we calibrate against the live site together.
"""
from __future__ import annotations

from pathlib import Path
from playwright.sync_api import Page, TimeoutError as PWTimeout

from ..safety import SafePage, SafetyConfig

HOST = "alensa.cz"
BASE_URL = "https://www.alensa.cz/"

# Common cookie-banner button texts on Czech sites.
COOKIE_ACCEPT_TEXTS = [
    "Souhlasím", "Přijmout vše", "Přijmout", "Rozumím",
    "Accept all", "Accept", "OK",
]


def open_homepage(page: Page, screenshot_path: Path | None = None) -> SafePage:
    safe = SafePage(page, SafetyConfig(allowed_host=HOST))
    safe.goto(BASE_URL, wait_until="domcontentloaded")
    accept_cookies(safe)
    if screenshot_path:
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(screenshot_path), full_page=False)
    return safe


def accept_cookies(safe: SafePage) -> bool:
    """Try a few common button texts. Returns True if something was clicked."""
    for text in COOKIE_ACCEPT_TEXTS:
        btn = safe.page.get_by_role("button", name=text, exact=False)
        try:
            if btn.count() > 0 and btn.first.is_visible(timeout=1000):
                safe.safe_click(btn.first, description=f"cookie:{text}")
                safe.page.wait_for_timeout(500)
                return True
        except PWTimeout:
            continue
        except Exception:
            continue
    return False
