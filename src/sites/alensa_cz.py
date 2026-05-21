"""alensa.cz site adapter.

Public API:
  open_homepage(page)              -> SafePage   open + accept cookies
  add_to_cart(safe, product_url)   -> None       navigate + click "Vložit do košíku"
  go_to_cart(safe)                 -> None       navigate to /nakup/kosik/
  read_cart_total(safe)            -> float      CZK total from cart subtotal
  apply_coupon(safe, code)         -> CouponResult
  proceed_to_checkout(safe)        -> None       click "Pokračovat v objednávce"

Hard rule: every navigation/click goes through SafePage so the safety guards
in src/safety.py keep us from accidentally placing an order.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from playwright.sync_api import Page, TimeoutError as PWTimeout

from ..safety import SafePage, SafetyConfig, SafetyViolation

HOST = "alensa.cz"
BASE_URL = "https://www.alensa.cz/"
CART_URL = "https://www.alensa.cz/nakup/kosik/"

COOKIE_ACCEPT_TEXTS = [
    "Pokračovat", "Souhlasím", "Přijmout vše", "Přijmout", "Rozumím",
    "Accept all", "Accept", "OK",
]

# Regex that parses prices like "1 234 Kč", "199 Kč", "1.234,50 Kč".
PRICE_RE = re.compile(
    r"(\d[\d\s ]*(?:[,.]\d+)?)\s*(?:Kč|CZK)", re.I
)


# ---------- bootstrap ----------

def open_homepage(page: Page, screenshot_path: Path | None = None,
                  dc_code: str | None = None) -> SafePage:
    """Open the homepage, optionally with a ?dc_code= coupon param applied.

    When dc_code is supplied, the site captures the code into session state on
    first page load — any product added afterwards inherits the discount in
    the cart. This is much simpler than the cart-side form-fill flow.

    Call read_pending_flash(safe) right after to capture the discount-applied
    modal text before it gets dismissed by subsequent navigation.
    """
    safe = SafePage(page, SafetyConfig(allowed_host=HOST))
    url = BASE_URL + (f"?dc_code={dc_code}" if dc_code else "")
    safe.goto(url, wait_until="domcontentloaded")
    accept_cookies(safe)
    if screenshot_path:
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(screenshot_path), full_page=False)
    return safe


def read_applied_coupon_info(safe: SafePage) -> Optional[str]:
    """Read the persistent 'applied coupon' chip on the cart page.

    Returns text like "Slevový kód: gel26care (-20%)", or None if no chip is
    present (meaning no coupon was applied). Must be called when on the cart
    page (/nakup/kosik/).
    """
    page = safe.page
    selectors = [
        "#snippet--couponCode .coupon-code-text",
        "#snippet--basketCouponDelivery .basket-coupon-info",
    ]
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if loc.count() > 0 and loc.is_visible(timeout=500):
                text = loc.inner_text(timeout=1000)
                cleaned = re.sub(r"\s+", " ", text).strip()
                if cleaned:
                    return cleaned
        except Exception:
            continue
    return None


def accept_cookies(safe: SafePage, timeout_ms: int = 5000) -> bool:
    """Dismiss the consentmanager.net banner via JS click.

    The banner has a BG overlay (#cmpbox2) that intercepts pointer events, so
    a normal click() is blocked. We dispatch the click in-page and wait for
    the banner to actually disappear.
    """
    page = safe.page
    try:
        page.wait_for_selector(".cmpboxbtnyes", state="visible", timeout=timeout_ms)
    except PWTimeout:
        return False
    text = page.locator(".cmpboxbtnyes").first.inner_text().strip()
    if not any(w.lower() in text.lower() for w in COOKIE_ACCEPT_TEXTS):
        return False
    page.evaluate("document.querySelector('.cmpboxbtnyes')?.click()")
    try:
        page.wait_for_selector("#cmpbox", state="hidden", timeout=timeout_ms)
        return True
    except PWTimeout:
        return False


# ---------- product / cart ----------

def add_to_cart(safe: SafePage, product_url: str) -> None:
    """Navigate to a product URL and click 'Vložit do košíku'.

    Does NOT handle required variant selection — call this for variant-free
    products only (eye drops, solution, accessories, etc.).
    """
    safe.goto(product_url, wait_until="domcontentloaded")
    safe.page.wait_for_timeout(800)
    # Banner sometimes reappears on a fresh navigation if consent wasn't saved.
    accept_cookies(safe, timeout_ms=2000)

    add_btn = safe.page.locator("a.detail-addToBasket-button").first
    add_btn.wait_for(state="visible", timeout=10_000)
    safe.safe_click(add_btn, description="add-to-cart")
    # The AJAX add updates the cart badge; the "Zobrazit košík" floating
    # button appears after a successful add. Wait for that to confirm the
    # add actually committed before we navigate away.
    try:
        safe.page.wait_for_selector(".go-to-basket-btn", state="visible", timeout=8_000)
    except PWTimeout:
        pass
    try:
        safe.page.wait_for_load_state("networkidle", timeout=8_000)
    except PWTimeout:
        pass
    safe.page.wait_for_timeout(500)


def go_to_cart(safe: SafePage) -> None:
    safe.goto(CART_URL, wait_until="domcontentloaded")
    safe.page.wait_for_timeout(500)


# ---------- price reading ----------

def parse_price_czk(text: str) -> float:
    """Parse a CZK price string into a float. Returns the first match found."""
    m = PRICE_RE.search(text)
    if not m:
        raise ValueError(f"No CZK price found in: {text!r}")
    raw = m.group(1).replace(" ", "").replace(" ", "").replace(",", ".")
    return float(raw)


def read_cart_total(safe: SafePage, timeout_ms: int = 15_000) -> float:
    """Read the cart subtotal ('Mezisoučet' or 'Celkem' on the cart page).

    Tries the snippet selector first, falls back to a generic .basket-total-price
    if the layout shifted, finally raises with a guess at the cart state.
    """
    page = safe.page
    selectors = [
        "#snippet--basketCompletePrice .basket-total-price",
        ".basket-total-price",
    ]
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            loc.wait_for(state="visible", timeout=timeout_ms)
            return parse_price_czk(loc.inner_text())
        except PWTimeout:
            continue
    body = ""
    try:
        body = page.inner_text("body")[:500]
    except Exception:
        pass
    empty_hint = "košík je prázdný" in body.lower() or "empty" in body.lower()
    raise RuntimeError(
        f"Cart total not visible after {timeout_ms}ms. "
        f"{'Cart appears empty.' if empty_hint else 'Page may have failed to render.'}"
    )


# ---------- coupon ----------

@dataclass
class CouponResult:
    code: str
    applied: bool
    total_before: float
    total_after: float
    discount_czk: float
    flash_message: Optional[str] = None  # site's response, if any

    @property
    def discount_pct(self) -> float:
        if self.total_before <= 0:
            return 0.0
        return round(100 * self.discount_czk / self.total_before, 2)


def _read_coupon_flash(page: Page) -> Optional[str]:
    """Find a status message from the coupon form (success or error)."""
    candidates = page.locator(
        "dialog.flash-message-dialog, "
        ".coupon-and-discounts-wrapper .flash, "
        ".coupon-and-discounts-wrapper p.error, "
        ".coupon-and-discounts-wrapper p.ok, "
        ".flash-message, "
        ".flash"
    )
    try:
        if candidates.count() > 0:
            text = candidates.first.inner_text(timeout=1000).strip()
            if text:
                return text[:500]
    except Exception:
        pass
    return None


def dismiss_flash_dialog(page: Page) -> bool:
    """Close any open <dialog class='flash-message-dialog'> that's blocking clicks.

    These auto-open after coupon apply / add-to-cart and intercept pointer
    events for the rest of the page. Close them in-JS to bypass the dialog
    backdrop, since clicking the close button can itself be intercepted.
    """
    dialog = page.locator("dialog.flash-message-dialog[open]")
    if dialog.count() == 0:
        return False
    page.evaluate(
        "document.querySelectorAll('dialog.flash-message-dialog[open]')"
        ".forEach(d => d.close())"
    )
    page.wait_for_timeout(200)
    return True


def apply_coupon(safe: SafePage, code: str) -> CouponResult:
    """Reveal the coupon field, type the code, submit, return before/after.

    Assumes we're already on the cart page (call go_to_cart first).
    """
    page = safe.page
    total_before = read_cart_total(safe)

    # Reveal the coupon form by clicking the label (the checkbox itself is
    # visually hidden). Skip if it's already revealed.
    form_visible = page.locator("form.couponForm").first
    if not form_visible.is_visible():
        label = page.locator("label[for='basket-coupon-checkbox']").first
        label.wait_for(state="visible", timeout=5_000)
        safe.safe_click(label, description="reveal-coupon")
        form_visible.wait_for(state="visible", timeout=5_000)

    code_input = page.locator("form.couponForm input[name='couponcode']").first
    submit_btn = page.locator("form.couponForm input[name='ok']").first
    code_input.fill(code)
    safe.safe_click(submit_btn, description="apply-coupon")

    # The form posts and re-renders. Wait for either the total to change or a
    # flash message to appear. We poll briefly.
    page.wait_for_load_state("networkidle", timeout=10_000)
    page.wait_for_timeout(1000)

    flash = _read_coupon_flash(page)
    dismiss_flash_dialog(page)
    total_after = read_cart_total(safe)
    discount = round(total_before - total_after, 2)
    return CouponResult(
        code=code,
        applied=discount > 0,
        total_before=total_before,
        total_after=total_after,
        discount_czk=discount,
        flash_message=flash,
    )


# ---------- checkout (stops before payment) ----------

def proceed_to_checkout(safe: SafePage) -> None:
    """Click 'Pokračovat v objednávce'. Safety guards take over from here.

    On the next page we MUST NOT click anything that submits the order. The
    SafePage wrapper will refuse pay-button clicks and abort on confirmation
    URLs. This function only advances one step.
    """
    dismiss_flash_dialog(safe.page)
    btn = safe.page.locator("a.btn-proceed").first
    btn.wait_for(state="visible", timeout=10_000)
    safe.safe_click(btn, description="proceed-to-checkout")
    safe.page.wait_for_load_state("domcontentloaded")
    safe.page.wait_for_timeout(1000)
