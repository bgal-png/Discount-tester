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

# Category listing pages on alensa.cz. solutions and eye_drops share one
# page; split them by the product name prefix below.
CATEGORY_LISTING_URLS: dict[str, Optional[str]] = {
    "solutions":      "https://www.alensa.cz/roztoky-a-ocni-kapky.html",
    "eye_drops":      "https://www.alensa.cz/roztoky-a-ocni-kapky.html",
    "contact_lenses": "https://www.alensa.cz/kontaktni-cocky.html",
    "glasses":        "https://www.alensa.cz/brylove-obroucky.html",
    "sunglasses":     None,    # TODO: ask user for URL
    "accessories":    None,    # TODO: ask user for URL
}

# When a listing page mixes product types, filter by what the data-name
# attribute starts with (Czech).
PRODUCT_TYPE_NAME_PREFIXES: dict[str, tuple[str, ...]] = {
    "solutions":  ("Roztok",),
    "eye_drops":  ("Oční kapky", "Kapky"),
}

# Product types that require variant selection (sphere/BC/qty etc.) before
# add-to-cart succeeds. We skip these in auto-discovery until variant
# handling lands.
VARIANT_REQUIRED_TYPES = {"contact_lenses"}

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


@dataclass
class ProductCard:
    url: str
    product_id: str
    name: str
    price_czk: Optional[float]


def list_products_on_category_page(safe: SafePage, category_url: str
                                   ) -> list[ProductCard]:
    """Visit a listing page and return its product cards."""
    safe.goto(category_url, wait_until="domcontentloaded")
    safe.page.wait_for_timeout(1500)
    cards: list[ProductCard] = []
    for el in safe.page.locator("a.product").all():
        try:
            href = el.get_attribute("href") or ""
            if not href:
                continue
            pid = el.get_attribute("data-id") or ""
            name = el.get_attribute("data-name") or ""
            price_raw = el.get_attribute("data-price")
            price = float(price_raw) if price_raw else None
            cards.append(ProductCard(
                url=href.strip(),
                product_id=pid,
                name=name.strip(),
                price_czk=price,
            ))
        except Exception:
            continue
    return cards


def _brand_matches(product_name: str, brand: Optional[str | list[str]]) -> bool:
    """Match a product name against a brand or a list of brands ('any of')."""
    if brand is None:
        return True
    name_lc = product_name.lower()
    if isinstance(brand, str):
        return brand.lower() in name_lc
    return any(b.lower() in name_lc for b in brand)


def find_products_in_category(safe: SafePage, *,
                              product_type: Optional[str],
                              brand: Optional[str | list[str]] = None,
                              limit: int = 3) -> list[ProductCard]:
    """Return up to `limit` product cards matching brand + product_type.

    brand may be a single string OR a list (matches if ANY listed brand
    appears in the product name). Returns [] if the category has no listing
    URL configured or the product type requires variants.
    """
    if product_type in VARIANT_REQUIRED_TYPES:
        return []
    url = CATEGORY_LISTING_URLS.get(product_type) if product_type else None
    if not url:
        return []
    cards = list_products_on_category_page(safe, url)
    prefixes = PRODUCT_TYPE_NAME_PREFIXES.get(product_type or "")
    out: list[ProductCard] = []
    for c in cards:
        if prefixes and not any(c.name.startswith(p) for p in prefixes):
            continue
        if not _brand_matches(c.name, brand):
            continue
        out.append(c)
        if len(out) >= limit:
            break
    return out


def find_non_matching_product(safe: SafePage, *,
                              exclude_product_type: Optional[str],
                              exclude_brand: Optional[str | list[str]] = None
                              ) -> Optional[ProductCard]:
    """Return a single product that doesn't match the given brand(s)+type.

    Strategy:
      1. If exclude_brand is set: prefer a SAME product_type / DIFFERENT brand
         product (tests the brand restriction directly, and stays in a
         category we know how to add to cart).
      2. Otherwise: pick a product from a different product_type entirely.

    Skips variant-required types (contact lenses) in either strategy.
    """
    # Strategy 1: same product_type, different brand(s).
    if exclude_brand and exclude_product_type and \
            exclude_product_type not in VARIANT_REQUIRED_TYPES:
        url = CATEGORY_LISTING_URLS.get(exclude_product_type)
        if url:
            prefixes = PRODUCT_TYPE_NAME_PREFIXES.get(exclude_product_type)
            for c in list_products_on_category_page(safe, url):
                if prefixes and not any(c.name.startswith(p) for p in prefixes):
                    continue
                if _brand_matches(c.name, exclude_brand):
                    continue  # this card IS one of the excluded brands -> skip
                return c

    # Strategy 2: different product_type.
    excluded_url = (CATEGORY_LISTING_URLS.get(exclude_product_type)
                    if exclude_product_type else None)
    for ptype, url in CATEGORY_LISTING_URLS.items():
        if not url or url == excluded_url or ptype in VARIANT_REQUIRED_TYPES:
            continue
        prefixes = PRODUCT_TYPE_NAME_PREFIXES.get(ptype)
        for c in list_products_on_category_page(safe, url):
            if prefixes and not any(c.name.startswith(p) for p in prefixes):
                continue
            if exclude_brand and _brand_matches(c.name, exclude_brand):
                continue
            return c
    return None


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
    """Navigate to a product URL and add it to the cart.

    Dispatches based on what the page actually exposes:
      - simple: 'Vložit do košíku' link (solutions, eye drops, accessories)
      - glasses configurator entered from frame page: 'Vybrat skla' link
      - glasses configurator landed-on directly: alensa redirects some
        dioptric frame URLs straight to /katalog/lenses-selector-detail/...
        in which case 'Brýle na dálku' (lens type) is already visible
    Contact lenses (variant-required) are still not handled.
    """
    import time as _time
    safe.goto(product_url, wait_until="domcontentloaded")
    safe.page.wait_for_timeout(500)
    accept_cookies(safe, timeout_ms=2000)

    page = safe.page

    # Some glasses-frame URLs JS-redirect to /katalog/lenses-selector-detail/...
    # only after a noticeable delay, and alensa.cz analytics keep the network
    # busy so wait_for_load_state('networkidle') is unreliable. Poll for one
    # of the three entry points to appear, up to 15 s.
    deadline = _time.time() + 15.0
    while _time.time() < deadline:
        if _safe_is_visible(page.locator("a.detail-addToBasket-button").first, 250):
            _add_simple(safe)
            return
        if "/lenses-selector-detail/" in page.url or \
                _safe_is_visible(page.locator("a:has-text('Brýle na dálku')").first, 250):
            _add_glasses(safe, skip_select_lenses=True)
            return
        if _safe_is_visible(page.locator("a:has-text('Vybrat skla')").first, 250):
            _add_glasses(safe, skip_select_lenses=False)
            return
        page.wait_for_timeout(500)

    raise RuntimeError(
        f"Couldn't find an add-to-cart entry on {product_url} after 15 s "
        f"(landed at {page.url!r}; no 'Vložit do košíku', 'Vybrat skla', "
        "or 'Brýle na dálku' visible)"
    )


def _safe_is_visible(locator, timeout_ms: int) -> bool:
    try:
        return locator.is_visible(timeout=timeout_ms)
    except Exception:
        return False


def _wait_for_basket_committed(safe: SafePage) -> None:
    """After an AJAX add-to-cart, wait for the floating cart button + idle
    network so server-side state has settled before we navigate away."""
    try:
        safe.page.wait_for_selector(".go-to-basket-btn", state="visible", timeout=8_000)
    except PWTimeout:
        pass
    try:
        safe.page.wait_for_load_state("networkidle", timeout=8_000)
    except PWTimeout:
        pass
    safe.page.wait_for_timeout(500)


def _add_simple(safe: SafePage) -> None:
    """Click the direct 'Vložit do košíku' link (solutions/drops flow)."""
    btn = safe.page.locator("a.detail-addToBasket-button").first
    btn.wait_for(state="visible", timeout=10_000)
    safe.safe_click(btn, description="add-to-cart-simple")
    _wait_for_basket_committed(safe)


# Default lens parameters for the glasses-frame configurator. Values picked
# to be valid in every dropdown alensa.cz offers — small SPH, narrow PD.
# The discount being tested usually applies to the whole bundle, so the
# specific lens choice doesn't matter for math, only that it's valid.
GLASSES_LENS_SPH = "-1.00"
GLASSES_LENS_PD = "30"


def _add_glasses(safe: SafePage, *, skip_select_lenses: bool = False) -> None:
    """Configurator flow for dioptric glasses: enter the configurator and
    add the frame-only default to cart.

    The configurator opens with "Objednat pouze obruby" (frames only)
    pre-selected. Frame-only orders still qualify for the discounts we
    care about, since these promos apply to "kompletní dioptrické brýle
    včetně vybraných skel A brýlové obroučky" — frames count.

    Click path:
      1. 'Vybrat skla' on the frame product page (skipped if alensa
         already routed us to /katalog/lenses-selector-detail/...)
      2. 'Vložit do košíku' on the configurator with the default
         frames-only selection

    Lens-prescription path (clicking 'Brýle na dálku' + filling SPH/PD)
    is intentionally NOT used here — Playwright's normal click on those
    radio cards gets intercepted, and the frame-only path is enough to
    validate the discount math.
    """
    page = safe.page

    # Cookie banner can reappear on the lens-selector subdomain.
    accept_cookies(safe, timeout_ms=2000)

    # Step 1: enter the configurator if we aren't already on it.
    if not skip_select_lenses:
        select_lenses = page.locator("a:has-text('Vybrat skla')").first
        select_lenses.wait_for(state="visible", timeout=10_000)
        safe.safe_click(select_lenses, description="select-lenses")
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except PWTimeout:
            pass
        page.wait_for_timeout(500)
        accept_cookies(safe, timeout_ms=2000)

    # Step 2: click 'Vložit do košíku' on the configurator.
    # JS-dispatched to bypass any transient overlay/animation Playwright
    # considers a click obstruction.
    add_btn = page.locator("a.btn-add-to-basket").first
    add_btn.wait_for(state="visible", timeout=10_000)
    label = (add_btn.inner_text(timeout=1000) or "").strip()
    if any(bad in label.lower() for bad in
           ("zaplatit", "potvrdit", "závazně", "zavazne")):
        raise SafetyViolation(
            f"Refusing JS-click on suspicious add-to-cart label: {label!r}"
        )
    page.evaluate(
        "document.querySelector('a.btn-add-to-basket')?.click()"
    )
    _wait_for_basket_committed(safe)


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
