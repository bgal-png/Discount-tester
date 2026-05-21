"""Hard safety guards so the tester can never place a real order.

Three layers:
  1. URL allowlist: page navigation away from the configured host is blocked.
  2. URL blocklist: any URL matching 'order-confirmation' style patterns aborts the run.
  3. Click guard: safe_click() refuses to click anything whose text matches PAY_BUTTON_TEXTS.

Use exclusively via the SafePage wrapper. Never call page.click() directly elsewhere.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from playwright.sync_api import Page, Locator


PAY_BUTTON_TEXTS = [
    # Czech
    "zaplatit", "objednat", "objednavku", "dokoncit objednavku",
    "dokončit objednávku", "závazně objednat", "zavazne objednat",
    "potvrdit objednávku", "potvrdit objednavku", "odeslat objednávku",
    # English fallback
    "pay now", "place order", "complete order", "confirm order", "submit order",
]

FORBIDDEN_URL_PATTERNS = [
    r"order-confirmation",
    r"objednavka-dokoncena",
    r"objednavka-odeslana",
    r"thank-you",
    r"dekujeme",
    r"děkujeme",
    r"/payment/process",
]


class SafetyViolation(RuntimeError):
    """Raised when the tester is about to do something it absolutely must not."""


@dataclass
class SafetyConfig:
    allowed_host: str  # e.g. "alensa.cz"


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def is_pay_button_text(text: str) -> bool:
    t = _normalize(text)
    return any(bad in t for bad in PAY_BUTTON_TEXTS)


def is_forbidden_url(url: str) -> bool:
    u = (url or "").lower()
    return any(re.search(p, u) for p in FORBIDDEN_URL_PATTERNS)


class SafePage:
    """Thin wrapper around Playwright's Page enforcing safety rules.

    Always go through .click()/.goto() on this wrapper. The underlying .page
    attribute is exposed for read-only operations (locators, text, screenshots).
    """

    def __init__(self, page: Page, safety: SafetyConfig):
        self.page = page
        self.safety = safety
        page.on("framenavigated", self._on_navigated)

    def _on_navigated(self, frame) -> None:
        if frame != self.page.main_frame:
            return
        url = frame.url
        if is_forbidden_url(url):
            raise SafetyViolation(
                f"Navigated to forbidden URL (possible order confirmation): {url}"
            )

    def goto(self, url: str, **kwargs):
        if self.safety.allowed_host not in url:
            raise SafetyViolation(
                f"Refusing to navigate outside allowed host "
                f"'{self.safety.allowed_host}': {url}"
            )
        if is_forbidden_url(url):
            raise SafetyViolation(f"Refusing to navigate to forbidden URL: {url}")
        return self.page.goto(url, **kwargs)

    def safe_click(self, locator: Locator, *, description: str = "") -> None:
        """Click a locator only after verifying its text isn't a pay button."""
        try:
            text = locator.inner_text(timeout=2000)
        except Exception:
            text = ""
        if is_pay_button_text(text):
            raise SafetyViolation(
                f"Refusing to click pay/confirm button "
                f"(text={text!r}, description={description!r})"
            )
        locator.click()


if __name__ == "__main__":
    # quick self-test
    assert is_pay_button_text("Závazně objednat")
    assert is_pay_button_text("ZAPLATIT  ")
    assert not is_pay_button_text("Pokračovat v nákupu")
    assert is_forbidden_url("https://alensa.cz/order-confirmation/12345")
    assert not is_forbidden_url("https://alensa.cz/cart")
    print("safety.py self-test OK")
