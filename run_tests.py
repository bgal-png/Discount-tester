"""Run all active discounts from config/discounts.json and report PASS/FAIL.

For each unique test_product_url among active discounts:
  1. Measure baseline (no code) in a fresh browser context.
For each active discount:
  2. Open homepage with ?dc_code=<code>, add the product, read cart total.
  3. Compute observed discount and compare to expected value.

Writes:
  reports/run_<ts>.json    structured report
  reports/run_<ts>.txt     human-readable summary

    python run_tests.py
    python run_tests.py --headed                # show the browser
    python run_tests.py --config other.json     # alternate config
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright, Playwright, BrowserContext

from src.config import load_config, Discount
from src.sites import alensa_cz
from src.safety import SafetyViolation


DIAG_DIR = Path("reports") / "diagnostics"

# alensa.cz occasionally fails to commit a cart add (especially the
# lens-configurator path's first attempt) — site flakiness, not a bug
# on our side. Retry the measurement once before giving up.
MEASUREMENT_RETRIES = 1


def with_retry(fn, *args, **kwargs):
    """Call fn(*args, **kwargs) up to MEASUREMENT_RETRIES + 1 times.

    Keyword `diag_out`, if present, gets a fresh dict each attempt so
    we only capture diagnostics from the FINAL (failing) attempt.
    Returns fn's result, or re-raises the last exception.
    """
    last_exc = None
    diag_out = kwargs.get("diag_out")
    label = kwargs.pop("_retry_label", "measurement")
    for attempt in range(MEASUREMENT_RETRIES + 1):
        # Fresh diag dict per attempt so earlier-attempt screenshots don't
        # mask the final failure state.
        if diag_out is not None:
            diag_out.clear()
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last_exc = e
            if attempt < MEASUREMENT_RETRIES:
                print(f"           [retry] {label} attempt {attempt + 1} "
                      f"failed ({type(e).__name__}); retrying...")
                continue
            raise
    raise last_exc  # unreachable, but mypy-friendly


def save_diagnostics(page, tag: str) -> dict:
    """Capture screenshot + HTML + URL/title when something goes wrong.

    Returns a dict that can be embedded in the result for the dashboard to
    render. Paths are relative to the project root.
    """
    DIAG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    stem = f"{tag}_{ts}"
    info = {"url": "", "title": "", "screenshot": "", "html": ""}
    try:
        info["url"] = page.url
        info["title"] = page.title()
    except Exception:
        pass
    try:
        path = DIAG_DIR / f"{stem}.png"
        page.screenshot(path=str(path), full_page=True)
        info["screenshot"] = str(path.as_posix())
    except Exception:
        pass
    try:
        path = DIAG_DIR / f"{stem}.html"
        path.write_text(page.content(), encoding="utf-8")
        info["html"] = str(path.as_posix())
    except Exception:
        pass
    return info


@dataclass
class MeasureWithCode:
    total: float
    flash_text: Optional[str]
    line_items: list[dict]  # serialised CartLineItems for the report


@dataclass
class DiscountTestResult:
    name: str
    code: str
    product_url: str
    expected_type: str        # "percentage" | "fixed_amount"
    expected_value: float
    tolerance_pct: float
    baseline_czk: Optional[float] = None
    observed_total_czk: Optional[float] = None
    observed_discount_czk: Optional[float] = None
    observed_discount_pct: Optional[float] = None
    line_items: list[dict] = field(default_factory=list)  # per-line cart breakdown
    flash_text: Optional[str] = None
    flash_check: Optional[str] = None        # "ok" | "missing:<phrase>" | "no_flash" | None
    negative_status: Optional[str] = None    # PASS_NOT_APPLIED | LEAKED | SKIPPED | ERROR
    negative_detail: Optional[str] = None
    status: str = "ERROR"     # PASS | FAIL | NOT_APPLIED | ERROR | SKIPPED_EXPIRED
    detail: str = ""
    duration_s: float = 0.0
    diagnostics: Optional[dict] = None       # set on failure: screenshot + URL/title


def new_context(p: Playwright, headed: bool) -> BrowserContext:
    browser = p.chromium.launch(headless=not headed, slow_mo=100 if headed else 0)
    return browser.new_context(locale="cs-CZ", viewport={"width": 1366, "height": 900})


def measure_baseline(p: Playwright, product_url: str, headed: bool,
                     diag_out: Optional[dict] = None,
                     cart_mode: str = "auto") -> float:
    """Add product to a clean cart, return the subtotal.

    cart_mode plumbed to alensa_cz.add_to_cart — must match the mode used
    for the with-code measurement so baseline and with-code are comparing
    the same basket composition.
    """
    ctx = new_context(p, headed)
    page = None
    try:
        page = ctx.new_page()
        safe = alensa_cz.open_homepage(page)
        alensa_cz.add_to_cart(safe, product_url, cart_mode=cart_mode)
        alensa_cz.go_to_cart(safe)
        return alensa_cz.read_cart_total(safe)
    except Exception:
        if diag_out is not None and page is not None:
            diag_out.update(save_diagnostics(page, "baseline_fail"))
        raise
    finally:
        ctx.close()


def measure_with_code(p: Playwright, product_url: str, code: str,
                      headed: bool, diag_out: Optional[dict] = None,
                      cart_mode: str = "auto") -> MeasureWithCode:
    """Apply the code via homepage URL param, then add product, then on the
    cart page read total, applied-coupon chip, and per-line items."""
    ctx = new_context(p, headed)
    page = None
    try:
        page = ctx.new_page()
        safe = alensa_cz.open_homepage(page, dc_code=code)
        alensa_cz.add_to_cart(safe, product_url, cart_mode=cart_mode)
        alensa_cz.go_to_cart(safe)
        total = alensa_cz.read_cart_total(safe)
        chip = alensa_cz.read_applied_coupon_info(safe)
        lines = alensa_cz.read_cart_line_items(safe)
        return MeasureWithCode(
            total=total,
            flash_text=chip,
            line_items=[
                {
                    "url_slug": ln.url_slug,
                    "name": ln.name,
                    "quantity": ln.quantity,
                    "original_unit_price_czk": ln.original_unit_price_czk,
                    "current_unit_price_czk": ln.current_unit_price_czk,
                    "is_discounted": ln.is_discounted,
                    "discount_pct": ln.discount_pct,
                    "discount_czk": ln.discount_czk,
                }
                for ln in lines
            ],
        )
    except Exception:
        if diag_out is not None and page is not None:
            diag_out.update(save_diagnostics(page, f"withcode_{code}_fail"))
        raise
    finally:
        ctx.close()


def check_flash(expected: list[str], observed: Optional[str]) -> str:
    """Return 'ok' if all expected substrings are in observed; else describe gap."""
    if not expected:
        return "skipped"  # nothing to check
    if not observed:
        return "no_flash"
    obs_lc = observed.lower()
    missing = [phrase for phrase in expected if phrase.lower() not in obs_lc]
    return "ok" if not missing else f"missing: {missing}"


def is_expired(d: Discount, today: date) -> bool:
    if not d.expires_at:
        return False
    try:
        return date.fromisoformat(d.expires_at) < today
    except ValueError:
        return False


def classify(d: Discount, baseline: float, observed_total: float,
             line_items: Optional[list[dict]] = None
             ) -> tuple[str, str, Optional[float]]:
    """Return (status, detail, observed_pct_used).

    Per-line check (preferred): if any cart line is discounted, the
    discount applies. We compare the LARGEST per-line discount % to the
    expected value — that's the line the coupon actually hit. This catches
    "discount applied to lens portion only" cases where the basket total
    shows a smaller % off.

    Falls back to basket-total math when no line shows is_discounted=True
    (older flow / sites that show only basket-level discounts).
    """
    discount = round(baseline - observed_total, 2)
    if discount <= 0:
        return "NOT_APPLIED", (
            f"observed total {observed_total} CZK == baseline {baseline} CZK; "
            "site did not honor the code"
        ), None

    # Per-line path.
    if line_items:
        discounted_lines = [ln for ln in line_items if ln.get("is_discounted")]
        if discounted_lines:
            # The line with the biggest applied % is the one the coupon
            # targeted; check that against expected.
            target = max(discounted_lines, key=lambda ln: ln["discount_pct"])
            observed_pct = target["discount_pct"]
            slug = target.get("url_slug") or target.get("name") or "?"
            if d.discount_type == "percentage":
                delta = abs(observed_pct - d.value)
                expected = f"{d.value:g}%"
                seen = f"{observed_pct:g}%"
                if delta <= d.tolerance_pct:
                    return "PASS", (
                        f"line '{slug}' got {seen} off (expected {expected}, "
                        f"delta {delta:.2f}pp ≤ {d.tolerance_pct}pp)"
                    ), observed_pct
                return "FAIL", (
                    f"line '{slug}' got {seen} off vs expected {expected} "
                    f"(delta {delta:.2f}pp > {d.tolerance_pct}pp)"
                ), observed_pct
            # fixed_amount per line
            line_czk_off = target.get("discount_czk", 0.0)
            delta_czk = abs(line_czk_off - d.value)
            allowed = max(1.0, d.value * d.tolerance_pct / 100)
            if delta_czk <= allowed:
                return "PASS", (
                    f"line '{slug}' got -{line_czk_off} CZK vs expected "
                    f"-{d.value} CZK"
                ), None
            return "FAIL", (
                f"line '{slug}' got -{line_czk_off} CZK vs expected "
                f"-{d.value} CZK (delta {delta_czk} CZK > {allowed})"
            ), None

    # Fallback: basket-total math.
    if d.discount_type == "percentage":
        observed_pct = round(100 * discount / baseline, 2) if baseline else 0
        delta = abs(observed_pct - d.value)
        if delta <= d.tolerance_pct:
            return "PASS", (
                f"basket-total: observed {observed_pct}% vs expected "
                f"{d.value}% (delta {delta:.2f}pp ≤ {d.tolerance_pct}pp)"
            ), observed_pct
        return "FAIL", (
            f"basket-total: observed {observed_pct}% vs expected "
            f"{d.value}% (delta {delta:.2f}pp > {d.tolerance_pct}pp)"
        ), observed_pct
    delta_czk = abs(discount - d.value)
    allowed = max(1.0, d.value * d.tolerance_pct / 100)
    if delta_czk <= allowed:
        return "PASS", (
            f"basket-total: observed -{discount} CZK vs expected "
            f"-{d.value} CZK"
        ), None
    return "FAIL", (
        f"basket-total: observed -{discount} CZK vs expected -{d.value} CZK "
        f"(delta {delta_czk} CZK > {allowed})"
    ), None


def cart_mode_for(d: Discount) -> str:
    """Pick the add-to-cart sub-flow appropriate for this discount.

    product_type may be a single string or a list. If 'lenses_for_glasses'
    is present, the with-lenses configurator path is needed; otherwise
    'auto' (the dispatcher picks per-page).
    """
    pt = d.applies_to.product_type
    if pt == "lenses_for_glasses":
        return "with_lenses"
    if isinstance(pt, list) and "lenses_for_glasses" in pt:
        return "with_lenses"
    return "auto"


def run_one(p: Playwright, d: Discount, product_url: str,
            negative_url: Optional[str], baselines: dict[str, float],
            headed: bool, mode: str = "auto") -> DiscountTestResult:
    """Run one (discount, product) test. baselines is keyed by
    '<url>#<cart_mode>' so that the same URL run in two modes carries two
    distinct baselines."""
    baseline = baselines[f"{product_url}#{mode}"]
    res = DiscountTestResult(
        name=d.name,
        code=d.code,
        product_url=product_url,
        expected_type=d.discount_type,
        expected_value=d.value,
        tolerance_pct=d.tolerance_pct,
        baseline_czk=baseline,
    )
    t0 = time.time()
    diag: dict = {}
    try:
        m = with_retry(
            measure_with_code, p, product_url, d.code, headed,
            diag_out=diag, cart_mode=mode,
            _retry_label=f"with-code {d.code} on {product_url}",
        )
        res.observed_total_czk = m.total
        res.observed_discount_czk = round(baseline - m.total, 2)
        res.line_items = m.line_items
        res.flash_text = m.flash_text
        res.flash_check = check_flash(d.expected_flash_contains, m.flash_text)
        res.status, res.detail, per_line_pct = classify(
            d, baseline, m.total, line_items=m.line_items
        )
        # Prefer the per-line % when available; fall back to basket % so
        # the report still shows something useful when no line is marked
        # discounted.
        if per_line_pct is not None:
            res.observed_discount_pct = per_line_pct
        elif baseline:
            res.observed_discount_pct = round(
                100 * res.observed_discount_czk / baseline, 2
            )

        if res.status == "PASS" and res.flash_check not in (None, "ok", "skipped"):
            res.status = "FAIL"
            res.detail = f"{res.detail}; flash {res.flash_check}"

        # Optional negative test.
        if negative_url:
            try:
                neg = with_retry(
                    measure_with_code, p, negative_url, d.code, headed,
                    cart_mode=mode,
                    _retry_label=f"negative {d.code} on {negative_url}",
                )
                neg_baseline = baselines.get(f"{negative_url}#{mode}") or with_retry(
                    measure_baseline, p, negative_url, headed, cart_mode=mode,
                    _retry_label=f"negative baseline {negative_url}",
                )
                leak = neg_baseline - neg.total
                leak_pct = 100 * leak / neg_baseline if neg_baseline else 0
                if leak_pct > 0.5:
                    res.negative_status = "LEAKED"
                    res.negative_detail = (
                        f"{negative_url} dropped {leak} CZK ({leak_pct:.1f}%); "
                        "restriction not enforced"
                    )
                    res.status = "FAIL"
                    res.detail = f"{res.detail}; NEGATIVE: {res.negative_detail}"
                else:
                    res.negative_status = "PASS_NOT_APPLIED"
                    res.negative_detail = (
                        f"{negative_url}: no discount (baseline {neg_baseline} "
                        f"vs with-code {neg.total})"
                    )
            except Exception as e:
                res.negative_status = "ERROR"
                res.negative_detail = f"{type(e).__name__}: {e}"
        else:
            res.negative_status = "SKIPPED"

    except SafetyViolation as e:
        res.status, res.detail = "ERROR", f"SAFETY: {e}"
        res.diagnostics = diag or None
    except Exception as e:
        res.status, res.detail = "ERROR", f"{type(e).__name__}: {e}"
        res.diagnostics = diag or None
    res.duration_s = round(time.time() - t0, 1)
    return res


def discover_products(p: Playwright, active: list[Discount], headed: bool,
                      limit_per_discount: int = 3,
                      sweep: bool = False
                      ) -> dict[str, tuple[list[str], Optional[str]]]:
    """For each active discount, resolve (positive_urls, negative_url).

    sweep=False (default): sample mode — N products from the default listing.
    sweep=True: focused sweep — N cheapest + N from default listing,
    designed to catch price-edge outlier bugs.

    Honors manual overrides (test_product_url / non_matching_product_url).
    Returns a dict keyed by discount.code.
    """
    out: dict[str, tuple[list[str], Optional[str]]] = {}
    # Use a single browser session for all scraping.
    ctx = new_context(p, headed)
    try:
        page = ctx.new_page()
        safe = alensa_cz.open_homepage(page)

        # Cache listings within this session so two discounts targeting the
        # same (brand, product_type) don't re-scrape.
        positive_cache: dict[tuple, list[str]] = {}
        negative_cache: dict[tuple, Optional[str]] = {}

        def _brand_key(b):
            return tuple(b) if isinstance(b, list) else b

        def _ptype_key(pt):
            return tuple(pt) if isinstance(pt, list) else pt

        for d in active:
            brand_k = _brand_key(d.applies_to.brand)
            # Positive products.
            if d.test_product_url:
                positives = [d.test_product_url]
            elif sweep:
                key = (brand_k, _ptype_key(d.applies_to.product_type), "sweep")
                if key not in positive_cache:
                    cards = alensa_cz.find_products_for_sweep(
                        safe,
                        brand=d.applies_to.brand,
                        product_type=d.applies_to.product_type,
                        n_cheapest=5,
                        n_default=5,
                    )
                    positive_cache[key] = [c.url for c in cards]
                positives = positive_cache[key]
            else:
                key = (brand_k, _ptype_key(d.applies_to.product_type), limit_per_discount)
                if key not in positive_cache:
                    cards = alensa_cz.find_products_in_category(
                        safe,
                        brand=d.applies_to.brand,
                        product_type=d.applies_to.product_type,
                        limit=limit_per_discount,
                    )
                    positive_cache[key] = [c.url for c in cards]
                positives = positive_cache[key]

            # Negative product.
            if d.non_matching_product_url:
                negative = d.non_matching_product_url
            else:
                nkey = (brand_k, _ptype_key(d.applies_to.product_type))
                if nkey not in negative_cache:
                    card = alensa_cz.find_non_matching_product(
                        safe,
                        exclude_brand=d.applies_to.brand,
                        exclude_product_type=d.applies_to.product_type,
                    )
                    negative_cache[nkey] = card.url if card else None
                negative = negative_cache[nkey]

            out[d.code] = (positives, negative)
    finally:
        ctx.close()
    return out


def main() -> None:
    # Force UTF-8 on stdout so Czech text and Unicode symbols don't crash
    # the Windows cp1250 default.
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/discounts.json")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument(
        "--push", action="store_true",
        help="After writing the report, git-commit + push it so the cloud "
             "dashboard sees it on next redeploy.",
    )
    ap.add_argument(
        "--only", nargs="+", metavar="CODE", default=None,
        help="Only run the discounts with these codes (ignores 'active' "
             "flag — useful for testing a single inactive discount).",
    )
    ap.add_argument(
        "--sweep", action="store_true",
        help="Focused sweep mode: test the 5 cheapest + 5 default-sort "
             "products per discount, instead of just 3. Catches price-edge "
             "outlier bugs (e.g. discount that wrongly applies to €1 promo "
             "items). Slower — ~10 min per discount instead of ~1 min.",
    )
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.only:
        wanted = {c.lower() for c in args.only}
        active = [d for d in cfg.discounts if d.code.lower() in wanted]
        unknown = wanted - {d.code.lower() for d in cfg.discounts}
        if unknown:
            print(f"ERROR: unknown code(s) in --only: {sorted(unknown)}")
            return
        print(f"--only filter: running {len(active)} of {len(cfg.discounts)} "
              f"configured discount(s), ignoring active flag")
    else:
        active = [d for d in cfg.discounts if d.active]
    if not active:
        print("No discounts to run — nothing to do.")
        return

    # A discount must EITHER have test_product_url set, OR have applies_to
    # set so we can auto-discover.
    missing = [d.code for d in active
               if not d.test_product_url
               and not (d.applies_to.brand or d.applies_to.product_type)]
    if missing:
        print(f"ERROR: these discounts need either test_product_url or "
              f"applies_to (brand/product_type) for auto-discovery: {missing}")
        return

    print(f"Loaded {len(active)} active discount(s) for {cfg.site}")

    reports = Path("reports")
    reports.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    results: list[DiscountTestResult] = []

    with sync_playwright() as p:
        # Phase 1: discover products (positive + negative) for each discount.
        mode_label = "sweep" if args.sweep else "sample"
        print(f"[discover] resolving product URLs for {len(active)} discount(s) "
              f"in {mode_label} mode...")
        per_discount = discover_products(p, active, args.headed, sweep=args.sweep)
        for d in active:
            positives, negative = per_discount[d.code]
            origin = "manual" if d.test_product_url else "auto"
            print(f"  {d.code}: {len(positives)} positive(s) [{origin}]"
                  + (", 1 negative" if negative else ""))
            for u in positives:
                print(f"     + {u}")
            if negative:
                print(f"     - {negative} (negative)")

        # Phase 2: baseline per (unique product, cart_mode). Same URL with
        # frame-only vs with-lenses gives different totals, so each needs
        # its own baseline. Keyed by "url#mode".
        baselines: dict[str, float] = {}
        baseline_diags: dict[str, dict] = {}
        baseline_keys: set[tuple[str, str]] = set()
        for d in active:
            mode = cart_mode_for(d)
            positives, negative = per_discount[d.code]
            for u in positives:
                baseline_keys.add((u, mode))
            if negative:
                baseline_keys.add((negative, mode))
        for url, mode in sorted(baseline_keys):
            key = f"{url}#{mode}"
            print(f"[baseline] {url} (cart_mode={mode})")
            diag: dict = {}
            try:
                baselines[key] = with_retry(
                    measure_baseline, p, url, args.headed,
                    diag_out=diag, cart_mode=mode,
                    _retry_label=f"baseline {url}",
                )
                print(f"           {baselines[key]} CZK")
            except Exception as e:
                print(f"           ERROR measuring baseline: {e}")
                if diag.get("screenshot"):
                    print(f"           diagnostic screenshot: {diag['screenshot']}")
                    print(f"           landed at: {diag.get('url')!r}  title: {diag.get('title')!r}")
                baselines[key] = float("nan")
                baseline_diags[key] = diag

        # Phase 3: per (discount, positive product) test.
        today = date.today()
        for d in active:
            if is_expired(d, today):
                print(f"[skip] {d.code} expired {d.expires_at}")
                results.append(DiscountTestResult(
                    name=d.name, code=d.code, product_url="",
                    expected_type=d.discount_type, expected_value=d.value,
                    tolerance_pct=d.tolerance_pct,
                    status="SKIPPED_EXPIRED",
                    detail=f"expired {d.expires_at}",
                ))
                continue

            positives, negative = per_discount[d.code]
            if not positives:
                print(f"[skip] {d.code} — no products discovered "
                      f"(brand={d.applies_to.brand}, type={d.applies_to.product_type})")
                results.append(DiscountTestResult(
                    name=d.name, code=d.code, product_url="",
                    expected_type=d.discount_type, expected_value=d.value,
                    tolerance_pct=d.tolerance_pct,
                    status="ERROR",
                    detail="no matching products found in listing (variant-required "
                           "types like contact_lenses are not yet supported)",
                ))
                continue

            mode = cart_mode_for(d)
            for product_url in positives:
                print(f"[test] {d.code} on {product_url} (cart_mode={mode})")
                baseline_key = f"{product_url}#{mode}"
                baseline = baselines[baseline_key]
                if baseline != baseline:  # NaN
                    results.append(DiscountTestResult(
                        name=d.name, code=d.code, product_url=product_url,
                        expected_type=d.discount_type, expected_value=d.value,
                        tolerance_pct=d.tolerance_pct,
                        status="ERROR", detail="baseline measurement failed",
                        diagnostics=baseline_diags.get(baseline_key) or None,
                    ))
                    continue
                results.append(run_one(p, d, product_url, negative,
                                       baselines, args.headed, mode=mode))
                print(f"       -> {results[-1].status}: {results[-1].detail}")

    # ---- Report ----
    summary = {
        "PASS": sum(1 for r in results if r.status == "PASS"),
        "FAIL": sum(1 for r in results if r.status == "FAIL"),
        "NOT_APPLIED": sum(1 for r in results if r.status == "NOT_APPLIED"),
        "ERROR": sum(1 for r in results if r.status == "ERROR"),
        "SKIPPED_EXPIRED": sum(1 for r in results if r.status == "SKIPPED_EXPIRED"),
    }
    out = {
        "timestamp": ts,
        "site": cfg.site,
        "currency": cfg.currency,
        "summary": summary,
        "results": [asdict(r) for r in results],
    }
    json_path = reports / f"run_{ts}.json"
    json_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    lines = [
        f"Run {ts} — {cfg.site}",
        f"Summary: {summary}",
        "",
    ]
    for r in results:
        lines.append(f"[{r.status:11s}] {r.code:15s} ({r.duration_s}s) — {r.detail}")
    txt_path = reports / f"run_{ts}.txt"
    txt_path.write_text("\n".join(lines), encoding="utf-8")

    print()
    print("\n".join(lines))
    print()
    print(f"Reports written:\n  {json_path}\n  {txt_path}")

    if args.push:
        push_to_github(json_path, txt_path, ts, summary)


def push_to_github(json_path: Path, txt_path: Path, ts: str, summary: dict) -> None:
    """Commit + push the new report so the cloud dashboard picks it up."""
    msg = f"Report {ts}: " + ", ".join(f"{k}={v}" for k, v in summary.items() if v)
    try:
        subprocess.run(["git", "add", str(json_path), str(txt_path)], check=True)
        # Skip if nothing actually staged (file was already pushed somehow).
        diff = subprocess.run(["git", "diff", "--cached", "--name-only"],
                              capture_output=True, text=True)
        if not diff.stdout.strip():
            print("(no changes to push)")
            return
        subprocess.run(["git", "commit", "-m", msg], check=True)
        subprocess.run(["git", "push"], check=True)
        print(f"Pushed report to GitHub: {msg}")
    except FileNotFoundError:
        print("git not found on PATH — skipping push. Install Git or push manually.")
    except subprocess.CalledProcessError as e:
        print(f"git push failed: {e}. Push manually with `git push` if you want "
              "the cloud dashboard to see this report.")


if __name__ == "__main__":
    main()
