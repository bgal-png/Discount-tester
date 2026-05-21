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

# Cookie-banner accept selectors (alensa.cz uses a "consentmanager.net"-style
# banner whose accept button has class `cmpboxbtnyes` and label "Pokračovat").
COOKIE_ACCEPT_SELECTORS = [
    "button.cmpboxbtnyes",
    "a.cmpboxbtnyes",
    ".cmpboxbtnyes",
]
COOKIE_ACCEPT_TEXTS = [
    "Pokračovat", "Souhlasím", "Přijmout vše", "Přijmout", "Rozumím",
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


def accept_cookies(safe: SafePage, timeout_ms: int = 5000) -> bool:
    """Accept the consentmanager.net banner.

    The banner is a modal with a background overlay (#cmpbox2) that intercepts
    pointer events, so a normal click() gets blocked. We use a JS-dispatched
    click on the accept anchor (.cmpboxbtnyes) and then wait for the banner
    container (#cmpbox) to disappear.
    """
    page = safe.page
    # Wait for the banner to actually render — it's injected by JS.
    try:
        page.wait_for_selector(".cmpboxbtnyes", state="visible", timeout=timeout_ms)
    except PWTimeout:
        return False  # no banner shown — already accepted, or not this site

    # Safety check: the button text should be a cookie-accept word, NOT a pay
    # word. If somehow it isn't, we'd rather skip than risk something weird.
    text = page.locator(".cmpboxbtnyes").first.inner_text().strip()
    if not any(w.lower() in text.lower() for w in COOKIE_ACCEPT_TEXTS):
        return False

    # JS click — bypasses pointer-events interception by the BG overlay.
    page.evaluate("document.querySelector('.cmpboxbtnyes')?.click()")
    try:
        page.wait_for_selector("#cmpbox", state="hidden", timeout=timeout_ms)
        return True
    except PWTimeout:
        return False
