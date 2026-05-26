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

# Note on the add-to-cart anchor selector — `a[href*='do=addToBasket']`:
# alensa uses different class combos per category. Solutions have an
# exact 'detail-addToBasket-button' class, sunglasses only have the longer
# 'datalayer-product-detail-addToBasket-button' which CSS `.foo` exact-
# class match wouldn't catch. The href shape `?itemid=N&do=addToBasket`
# is consistent across all product categories, so we match by that.

# Category listing pages on alensa.cz. solutions and eye_drops share one
# page; split them by the product name prefix below.
CATEGORY_LISTING_URLS: dict[str, Optional[str]] = {
    "solutions":           "https://www.alensa.cz/roztoky-a-ocni-kapky.html",
    "eye_drops":           "https://www.alensa.cz/roztoky-a-ocni-kapky.html",
    "contact_lenses":      "https://www.alensa.cz/kontaktni-cocky.html",
    "glasses":             "https://www.alensa.cz/brylove-obroucky.html",
    # Spectacle lenses aren't sold standalone; to test a lens discount we
    # pick a glasses frame and add it WITH lenses in the configurator.
    "lenses_for_glasses":  "https://www.alensa.cz/brylove-obroucky.html",
    "sunglasses":          "https://www.alensa.cz/slunecni-bryle.html",
    "glasses_accessories": "https://www.alensa.cz/prislusenstvi-k-brylim.html",
    "lens_accessories":    "https://www.alensa.cz/prislusenstvi-k-cockam.html",
    # Legacy alias — defaults to lens accessories (the larger category
    # for an eshop centred on contact lenses).
    "accessories":         "https://www.alensa.cz/prislusenstvi-k-cockam.html",
}

# When a listing page mixes product types, filter by what the data-name
# attribute starts with (Czech).
PRODUCT_TYPE_NAME_PREFIXES: dict[str, tuple[str, ...]] = {
    "solutions":  ("Roztok",),
    "eye_drops":  ("Oční kapky", "Kapky"),
}

# Product types that require variant selection but we DON'T yet handle.
# Empty for now — contact_lenses are handled via _add_contact_lens.
VARIANT_REQUIRED_TYPES: set[str] = set()

# Maps product_type to the base path used in brand-filtered URLs.
# Format: /<base-path>/<brand-slug> returns a brand-filtered listing.
# Verified: /brylove-obroucky/crulle works, /kontaktni-cocky/<brand> works.
BRAND_FILTER_PATHS: dict[str, str] = {
    "glasses":             "/brylove-obroucky",
    "sunglasses":          "/slunecni-bryle",
    "contact_lenses":      "/kontaktni-cocky",
    "lenses_for_glasses":  "/brylove-obroucky",  # same listing as frames
    "solutions":           None,   # no brand-filtered listing — name prefix suffices
    "eye_drops":           None,
    "glasses_accessories": None,
    "lens_accessories":    None,
    "accessories":         None,
}


def _slugify_brand(brand: str) -> str:
    """Brand-name -> URL slug used in /<cat>/<slug> brand-filter listings.
    - Strips accents: 'Crullé' -> 'crulle'
    - Spaces to hyphens: 'Hugo by Hugo Boss' -> 'hugo-by-hugo-boss'
    - Apostrophes also become hyphens: "Levi's" -> 'levi-s'
    - Collapses runs of dashes.
    """
    import re
    import unicodedata
    s = unicodedata.normalize("NFKD", brand).encode("ascii", "ignore").decode("ascii")
    s = s.lower().strip()
    s = re.sub(r"[\s'_]+", "-", s)   # whitespace, apostrophe, underscore -> hyphen
    s = re.sub(r"-+", "-", s)        # collapse multiple hyphens
    return s.strip("-")


# URL suffix that re-orders a listing page from cheapest to most expensive.
# Discovered empirically: clicking the "Nejnižší cena" option in the
# .catalogue-sorting-select dropdown redirects to this query param.
# The mirror URL `?seradit=nejvyssi-cena` returns empty (probably an AJAX
# endpoint when accessed directly); for now we approximate "expensive"
# by paginating the cheapest sort or just skipping that bucket.
LISTING_SORT_CHEAPEST = "?seradit=nejnizsi-cena"

# Default contact-lens sphere/diopter to pick when the page doesn't pre-select
# one. Most lens products carry "-1.00" as a valid option. The runner falls
# back to "the first non-placeholder option" if -1.00 isn't listed.
CONTACT_LENS_DEFAULT_SPH = "-1.00"

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


def _parse_data_price(raw: Optional[str]) -> Optional[float]:
    """data-price comes in three shapes: '899' (cheap), '1 999' with a
    non-breaking thousand-separator (expensive), '1 999,50' with comma
    decimal. Normalise all of them."""
    if not raw:
        return None
    cleaned = raw.replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def list_products_on_category_page(safe: SafePage, category_url: str
                                   ) -> list[ProductCard]:
    """Visit a listing page and return its product cards.

    Waits until the first product card actually appears (up to 8 s) rather
    than a fixed delay — some brand-filtered listings render slowly. If
    nothing appears in 8 s, treats the listing as empty (out of stock).
    """
    safe.goto(category_url, wait_until="domcontentloaded")
    try:
        safe.page.wait_for_selector("a.product", state="visible", timeout=8_000)
    except PWTimeout:
        return []  # truly empty listing — out of stock or wrong URL
    safe.page.wait_for_timeout(500)  # small buffer for the remaining cards
    cards: list[ProductCard] = []
    for el in safe.page.locator("a.product").all():
        try:
            href = el.get_attribute("href") or ""
            if not href:
                continue
            pid = el.get_attribute("data-id") or ""
            name = el.get_attribute("data-name") or ""
            price = _parse_data_price(el.get_attribute("data-price"))
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


def _types_list(product_type: Optional[str | list[str]]) -> list[Optional[str]]:
    """Normalise product_type into a list. None -> [None]; str -> [str];
    list -> the list. Lets find_* functions loop uniformly."""
    if product_type is None:
        return [None]
    if isinstance(product_type, str):
        return [product_type]
    return list(product_type) or [None]


def find_products_in_category(safe: SafePage, *,
                              product_type: Optional[str | list[str]],
                              brand: Optional[str | list[str]] = None,
                              limit: int = 3) -> list[ProductCard]:
    """Return up to `limit` product cards matching brand + product_type.

    product_type may be a single string OR a list (e.g. ['glasses',
    'sunglasses']) — listings are visited in order, results unioned,
    de-duped by URL. brand similarly accepts a single string or a list.
    Variant-required types (currently empty) are skipped.
    """
    out: list[ProductCard] = []
    seen_urls: set[str] = set()
    for one_type in _types_list(product_type):
        if len(out) >= limit:
            break
        if one_type in VARIANT_REQUIRED_TYPES:
            continue
        url = CATEGORY_LISTING_URLS.get(one_type) if one_type else None
        if not url:
            continue
        cards = list_products_on_category_page(safe, url)
        prefixes = PRODUCT_TYPE_NAME_PREFIXES.get(one_type or "")
        for c in cards:
            if c.url in seen_urls:
                continue
            if prefixes and not any(c.name.startswith(p) for p in prefixes):
                continue
            if not _brand_matches(c.name, brand):
                continue
            out.append(c)
            seen_urls.add(c.url)
            if len(out) >= limit:
                break
    return out


def find_products_for_sweep(safe: SafePage, *,
                            product_type: Optional[str | list[str]],
                            brand: Optional[str | list[str]] = None,
                            n_cheapest: int = 5,
                            n_default: int = 10,
                            n_per_brand: int = 1) -> list[ProductCard]:
    """Stratified sample for focused-sweep mode.

    Per product_type (loops if a list is given):
      - up to `n_cheapest` from the price-ascending listing
      - up to `n_default` from the default popularity sort
      - up to `n_per_brand` from EACH brand-filtered listing (only when
        `brand` is a list of >1 brands — catches per-brand backend-tagging
        bugs)

    De-dups by URL across the whole result.
    """
    seen_urls: set[str] = set()
    picks: list[ProductCard] = []

    for one_type in _types_list(product_type):
        if one_type in VARIANT_REQUIRED_TYPES:
            continue
        base_url = CATEGORY_LISTING_URLS.get(one_type) if one_type else None
        if not base_url:
            continue

        prefixes = PRODUCT_TYPE_NAME_PREFIXES.get(one_type or "")

        def _matches(card: ProductCard, _prefixes=prefixes) -> bool:
            if _prefixes and not any(card.name.startswith(p) for p in _prefixes):
                return False
            return _brand_matches(card.name, brand)

        def _add_from(cards: list[ProductCard], limit: int) -> int:
            added = 0
            for c in cards:
                if not _matches(c) or c.url in seen_urls:
                    continue
                picks.append(c)
                seen_urls.add(c.url)
                added += 1
                if added >= limit:
                    break
            return added

        # Bucket 1: cheapest first.
        _add_from(
            list_products_on_category_page(safe, base_url + LISTING_SORT_CHEAPEST),
            n_cheapest,
        )

        # Bucket 2: default sort.
        _add_from(list_products_on_category_page(safe, base_url), n_default)

        # Bucket 3: per-brand listing for THIS type.
        brand_path = BRAND_FILTER_PATHS.get(one_type or "")
        if brand_path and isinstance(brand, list) and len(brand) > 1:
            for one_brand in brand:
                slug = _slugify_brand(one_brand)
                url = f"https://www.alensa.cz{brand_path}/{slug}"
                try:
                    cards = list_products_on_category_page(safe, url)
                except Exception:
                    continue
                added = 0
                for c in cards:
                    if c.url in seen_urls:
                        continue
                    if prefixes and not any(c.name.startswith(p) for p in prefixes):
                        continue
                    picks.append(c)
                    seen_urls.add(c.url)
                    added += 1
                    if added >= n_per_brand:
                        break

    return picks


def find_non_matching_product(safe: SafePage, *,
                              exclude_product_type: Optional[str | list[str]],
                              exclude_brand: Optional[str | list[str]] = None
                              ) -> Optional[ProductCard]:
    """Return a single product that doesn't match the given brand(s)+type(s).

    Strategy:
      1. If exclude_brand is set: prefer SAME product_type / DIFFERENT brand
         (tests the brand restriction directly, stays in a cart-flow we know).
      2. Otherwise: pick a product from a different product_type entirely.

    Skips variant-required types in either strategy.
    """
    excluded_types = [t for t in _types_list(exclude_product_type) if t is not None]
    excluded_urls = {CATEGORY_LISTING_URLS.get(t)
                     for t in excluded_types
                     if CATEGORY_LISTING_URLS.get(t)}

    # Strategy 1: same product_type(s), different brand. Iterate each excluded
    # type in turn — the first one with a usable cart flow + non-matching
    # brand wins.
    if exclude_brand:
        for ptype in excluded_types:
            if ptype in VARIANT_REQUIRED_TYPES:
                continue
            url = CATEGORY_LISTING_URLS.get(ptype)
            if not url:
                continue
            prefixes = PRODUCT_TYPE_NAME_PREFIXES.get(ptype)
            for c in list_products_on_category_page(safe, url):
                if prefixes and not any(c.name.startswith(p) for p in prefixes):
                    continue
                if _brand_matches(c.name, exclude_brand):
                    continue  # IS one of the excluded brands -> skip
                return c

    # Strategy 2: different product_type entirely.
    for ptype, url in CATEGORY_LISTING_URLS.items():
        if not url or url in excluded_urls or ptype in VARIANT_REQUIRED_TYPES:
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

def add_to_cart(safe: SafePage, product_url: str,
                cart_mode: str = "auto") -> None:
    """Navigate to a product URL and add it to the cart.

    cart_mode:
      - "auto" (default): on glasses, order frame-only (skips lens config)
      - "with_lenses": on glasses, fully configure single-vision lenses
        (Brýle na dálku, SPH = -1.00 both eyes, PD = 30 both eyes)

    Dispatches based on what the page actually exposes:
      - simple: 'Vložit do košíku' link (solutions, eye drops, accessories)
      - glasses configurator entered from frame page: 'Vybrat skla' link
      - glasses configurator landed-on directly via /lenses-selector-detail/
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
    # of the entry points to appear, up to 15 s.
    #
    # Order matters — we MUST check the glasses-configurator signals (URL
    # pattern, 'Brýle na dálku', 'Vybrat skla') BEFORE the generic
    # `a[href*='do=addToBasket']` selector. Glasses pages can briefly
    # render with a frame-only AJAX add link before the redirect kicks in,
    # and we don't want to fall into _add_simple for what's really a
    # frame+lens bundle test.
    deadline = _time.time() + 15.0
    while _time.time() < deadline:
        # Contact-lens product pages carry the sphere selector inline.
        if _safe_is_visible(page.locator("select[name='primary[7]']").first, 250):
            _add_contact_lens(safe)
            return
        # Glasses configurator landed-state.
        if "/lenses-selector-detail/" in page.url or \
                _safe_is_visible(page.locator("a:has-text('Brýle na dálku')").first, 250):
            _add_glasses(safe, skip_select_lenses=True, cart_mode=cart_mode)
            return
        # Glasses frame product page (configurator entry point).
        if _safe_is_visible(page.locator("a:has-text('Vybrat skla')").first, 250):
            _add_glasses(safe, skip_select_lenses=False, cart_mode=cart_mode)
            return
        # Generic simple-add — last resort, matches any AJAX add anchor
        # (solutions, drops, accessories, sunglasses, contact lenses with
        # variants already picked).
        if _safe_is_visible(page.locator("a[href*='do=addToBasket']").first, 250):
            _add_simple(safe)
            return
        page.wait_for_timeout(500)

    raise RuntimeError(
        f"Couldn't find an add-to-cart entry on {product_url} after 15 s "
        f"(landed at {page.url!r}; no 'Vložit do košíku', 'Vybrat skla', "
        "'Brýle na dálku', or sphere selector visible)"
    )


def _add_contact_lens(safe: SafePage) -> None:
    """Contact-lens product flow: pick a sphere on the product page, then
    use the standard add-to-cart link.

    The variant selectors are right on the product page (no configurator
    subpage like glasses). primary[7] is the sphere/diopter; quantity
    (primary[amount]) and BC/DIA (primary[8disabled], primary[9disabled])
    usually have sensible defaults. We only set sphere.
    """
    page = safe.page

    # Try the canonical default first; fall back to the first non-placeholder
    # option if this product doesn't carry it.
    sel_loc = page.locator("select[name='primary[7]']").first
    sel_loc.wait_for(state="visible", timeout=10_000)
    try:
        sel_loc.select_option(label=CONTACT_LENS_DEFAULT_SPH, timeout=3000)
    except Exception:
        # JS: pick the first option whose text is a numeric diopter
        # (skip placeholders like "---", "Vyberte ...", etc.).
        page.evaluate(
            """
            (() => {
              const sel = document.querySelector("select[name='primary[7]']");
              if (!sel) return;
              const opt = Array.from(sel.options).find(o => /^[-+]?\\d+(\\.\\d+)?$/.test(o.text.trim()));
              if (!opt) return;
              sel.value = opt.value;
              sel.dispatchEvent(new Event('change', {bubbles: true}));
            })();
            """
        )

    # Let any AJAX driven by the sphere change settle (some products show
    # different add-button hrefs once a variant is picked).
    try:
        page.wait_for_load_state("networkidle", timeout=6_000)
    except PWTimeout:
        pass
    page.wait_for_timeout(500)

    # Now the standard add-to-cart anchor should be active.
    btn = page.locator("a[href*='do=addToBasket']").first
    btn.wait_for(state="visible", timeout=10_000)
    safe.safe_click(btn, description="add-to-cart-contact-lens")
    _wait_for_basket_committed(safe)


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
    btn = safe.page.locator("a[href*='do=addToBasket']").first
    btn.wait_for(state="visible", timeout=10_000)
    safe.safe_click(btn, description="add-to-cart-simple")
    _wait_for_basket_committed(safe)


# Default lens parameters for the glasses-frame configurator. Values picked
# to be valid in every dropdown alensa.cz offers — small SPH, narrow PD.
# The discount being tested usually applies to the whole bundle, so the
# specific lens choice doesn't matter for math, only that it's valid.
GLASSES_LENS_SPH = "-1.00"
GLASSES_LENS_PD = "30"


def _add_glasses(safe: SafePage, *, skip_select_lenses: bool = False,
                 cart_mode: str = "auto") -> None:
    """Configurator flow for dioptric glasses.

    cart_mode="auto": leave the default 'Objednat pouze obruby' (frame only)
        selected and click 'Vložit do košíku'. Cheapest / fastest path.
    cart_mode="with_lenses": JS-click 'Brýle na dálku' to switch to single-
        vision lenses, fill SPH (-1.00) and PD (30) for both eyes, then add.
        Used for discounts targeting product_type='lenses_for_glasses'.

    Click path:
      1. 'Vybrat skla' on the frame product page (skipped if alensa
         already routed us to /katalog/lenses-selector-detail/...)
      2. (with_lenses only) Pick 'Brýle na dálku' + fill SPH/PD
      3. 'Vložit do košíku' on the configurator
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

    # Step 2 (with_lenses only): switch to single-vision lenses + fill
    # prescription. Normal Playwright clicks on the radio cards get
    # intercepted (~30 s timeout), so we use JS dispatch.
    if cart_mode == "with_lenses":
        _configure_lenses(safe)

    # Step 3: click 'Vložit do košíku' on the configurator. JS-dispatched
    # to bypass any transient overlay/animation Playwright considers a
    # click obstruction.
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


def _configure_lenses(safe: SafePage) -> None:
    """JS-driven: pick 'Brýle na dálku', fill SPH=-1.00 + PD=30 both eyes.

    The site has a known UX bug: after the AJAX updates from picking the
    lens type and filling SPH/PD, the 'Vložit do košíku' button stays in
    a non-functional state until the page is reloaded. Each AJAX call
    encodes the configuration into the URL, so a reload preserves state.

    Default selections in sections 4 (lens type = BASIC, +498 Kč), 5
    (surface treatment = Základní úprava, +0 Kč), and 6 (special treatment
    = Základní úpravy, +0 Kč) are accepted without interaction — per
    user-confirmed walkthrough they're sensibly pre-selected.
    """
    page = safe.page

    # Pick "Brýle na dálku" (single-vision distance). JS-click bypasses the
    # overlay that intercepts Playwright's normal click.
    distance = page.locator("a.type-wrapper-with-image:has-text('Brýle na dálku')").first
    distance.wait_for(state="visible", timeout=10_000)
    page.evaluate(
        "Array.from(document.querySelectorAll('a.type-wrapper-with-image'))"
        ".find(a => a.textContent.includes('Brýle na dálku'))?.click()"
    )
    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except PWTimeout:
        pass
    page.wait_for_timeout(1500)

    # Each select change fires a server-side AJAX that updates state. If
    # we fire them too fast, an in-flight request from the previous select
    # is replaced by the next one, and the first eye's parameter ends up
    # blank in the saved state. Wait for AJAX to settle between each.
    def _settle():
        try:
            page.wait_for_load_state("networkidle", timeout=6_000)
        except PWTimeout:
            pass
        page.wait_for_timeout(500)

    _set_select_by_label(page, "select.primary-sph", GLASSES_LENS_SPH)
    _settle()
    _set_select_by_label(page, "select.secondary-sph", GLASSES_LENS_SPH)
    _settle()
    _set_select_by_value(page, "select.primary-pd", GLASSES_LENS_PD)
    _settle()
    _set_select_by_value(page, "select.secondary-pd", GLASSES_LENS_PD)
    _settle()

    # The hydration-bug workaround: reload so the add-to-cart button
    # becomes functional. URL holds the config, so we land back on a
    # fully-configured page just with a live button.
    safe.page.reload(wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except PWTimeout:
        pass
    page.wait_for_timeout(800)
    # Cookie banner can re-appear on the reloaded URL.
    accept_cookies(safe, timeout_ms=2000)


def _set_select_by_label(page: Page, selector: str, label: str) -> None:
    """Pick the option whose visible text matches `label`. Tries Playwright
    first (short timeout), then JS as a fallback."""
    loc = page.locator(selector).first
    try:
        loc.select_option(label=label, timeout=4000)
        return
    except Exception:
        pass
    page.evaluate(
        f"""
        (() => {{
          const sel = document.querySelector({selector!r});
          if (!sel) return;
          const opt = Array.from(sel.options).find(o => o.text.trim() === {label!r});
          if (!opt) return;
          sel.value = opt.value;
          sel.dispatchEvent(new Event('change', {{bubbles: true}}));
        }})();
        """
    )


def _set_select_by_value(page: Page, selector: str, value: str) -> None:
    loc = page.locator(selector).first
    try:
        loc.select_option(value=value, timeout=4000)
        return
    except Exception:
        pass
    page.evaluate(
        f"""
        (() => {{
          const sel = document.querySelector({selector!r});
          if (!sel) return;
          sel.value = {value!r};
          sel.dispatchEvent(new Event('change', {{bubbles: true}}));
        }})();
        """
    )


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


@dataclass
class CartLineItem:
    """A single line on the cart page (one product + its current+original prices)."""
    url_slug: str
    name: str
    quantity: int
    original_unit_price_czk: float
    current_unit_price_czk: float  # what the customer actually pays per unit
    is_discounted: bool            # True when product-price-before showed up

    @property
    def line_total_czk(self) -> float:
        return self.current_unit_price_czk * self.quantity

    @property
    def discount_pct(self) -> float:
        """Per-line discount %, 0 if not discounted."""
        if self.original_unit_price_czk <= 0 or not self.is_discounted:
            return 0.0
        delta = self.original_unit_price_czk - self.current_unit_price_czk
        return round(100 * delta / self.original_unit_price_czk, 2)

    @property
    def discount_czk(self) -> float:
        """Per-unit CZK discount, 0 if not discounted."""
        if not self.is_discounted:
            return 0.0
        return round(self.original_unit_price_czk - self.current_unit_price_czk, 2)


def read_cart_line_items(safe: SafePage) -> list[CartLineItem]:
    """Read each `<div class="product-basket-wrapper">` on the cart page.

    The HTML pattern alensa uses:
      .product-basket-wrapper                  one per cart line
        .product-name-area .product-name a     name + href -> url_slug
        .product-prices-area
          .product-price-before                ORIGINAL price, only shown when discounted
          .product-price                       current price (after discount)
        select[name="primary[amount]"]         quantity (selected option)

    Skips wrappers that don't look like real line items (e.g. gift slots).
    Returns [] if the cart is empty.
    """
    page = safe.page
    out: list[CartLineItem] = []

    # Single-product carts use data-item="global"; glasses-with-lenses
    # bundles split into two lines, one with data-item="glasses" (frame)
    # and one with data-item="global" (the lens pair, where the discount
    # actually applies for 25zskla-style codes).
    for el in page.locator(
        "div.product-basket-wrapper[data-item='global'], "
        "div.product-basket-wrapper[data-item='glasses']"
    ).all():
        try:
            name_loc = el.locator(".product-name-area .product-name").first
            if name_loc.count() == 0:
                continue
            # Anchor inside h3 is preferred (gives us a URL slug), but
            # the lens line renders plain text inside h3.
            link_loc = el.locator(".product-name-area .product-name a").first
            if link_loc.count() > 0:
                href = (link_loc.get_attribute("href") or "").strip()
                url_slug = href.rstrip("/").rsplit("/", 1)[-1] if href else ""
                name = (link_loc.inner_text(timeout=500) or "").strip()
            else:
                url_slug = ""
                name = (name_loc.inner_text(timeout=500) or "").strip()

            # Current price: the .product-price inside .product-prices-area
            # (NOT the hidden gtm-data one).
            current_loc = el.locator(
                ".product-prices-area .product-price"
            ).first
            try:
                current_price = parse_price_czk(
                    current_loc.inner_text(timeout=500)
                )
            except Exception:
                continue  # can't read line, skip

            # Original price (only present when discounted).
            before_loc = el.locator(
                ".product-prices-area .product-price-before"
            ).first
            try:
                original_price = parse_price_czk(
                    before_loc.inner_text(timeout=500)
                )
                is_discounted = (
                    abs(original_price - current_price) > 0.01
                )
            except Exception:
                original_price = current_price
                is_discounted = False

            # Quantity from the amount selector.
            try:
                qty_value = el.locator(
                    "select[name='primary[amount]']"
                ).first.evaluate("e => e.value")
                quantity = int(qty_value) if qty_value else 1
            except Exception:
                quantity = 1

            out.append(CartLineItem(
                url_slug=url_slug,
                name=name,
                quantity=quantity,
                original_unit_price_czk=original_price,
                current_unit_price_czk=current_price,
                is_discounted=is_discounted,
            ))
        except Exception:
            continue

    return out


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
