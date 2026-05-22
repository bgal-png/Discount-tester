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
    cart page read both the total and the persistent applied-coupon chip."""
    ctx = new_context(p, headed)
    page = None
    try:
        page = ctx.new_page()
        safe = alensa_cz.open_homepage(page, dc_code=code)
        alensa_cz.add_to_cart(safe, product_url, cart_mode=cart_mode)
        alensa_cz.go_to_cart(safe)
        total = alensa_cz.read_cart_total(safe)
        chip = alensa_cz.read_applied_coupon_info(safe)
        return MeasureWithCode(total=total, flash_text=chip)
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


def classify(d: Discount, baseline: float, observed_total: float) -> tuple[str, str]:
    """Return (status, detail) by comparing observed discount to expected."""
    discount = round(baseline - observed_total, 2)
    if discount <= 0:
        return "NOT_APPLIED", (
            f"observed total {observed_total} CZK == baseline {baseline} CZK; "
            "site did not honor the code"
        )

    if d.discount_type == "percentage":
        observed_pct = round(100 * discount / baseline, 2) if baseline else 0
        delta = abs(observed_pct - d.value)
        if delta <= d.tolerance_pct:
            return "PASS", f"observed {observed_pct}% vs expected {d.value}% (delta {delta:.2f}pp ≤ {d.tolerance_pct}pp)"
        return "FAIL", f"observed {observed_pct}% vs expected {d.value}% (delta {delta:.2f}pp > {d.tolerance_pct}pp)"

    # fixed_amount
    delta_czk = abs(discount - d.value)
    # treat tolerance_pct as percent of expected amount for fixed discounts
    allowed = max(1.0, d.value * d.tolerance_pct / 100)
    if delta_czk <= allowed:
        return "PASS", f"observed -{discount} CZK vs expected -{d.value} CZK (delta {delta_czk} CZK ≤ {allowed})"
    return "FAIL", f"observed -{discount} CZK vs expected -{d.value} CZK (delta {delta_czk} CZK > {allowed})"


def cart_mode_for(d: Discount) -> str:
    """Pick the add-to-cart sub-flow appropriate for this discount."""
    if d.applies_to.product_type == "lenses_for_glasses":
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
        m = measure_with_code(p, product_url, d.code, headed,
                              diag_out=diag, cart_mode=mode)
        res.observed_total_czk = m.total
        res.observed_discount_czk = round(baseline - m.total, 2)
        if baseline:
            res.observed_discount_pct = round(100 * res.observed_discount_czk / baseline, 2)
        res.flash_text = m.flash_text
        res.flash_check = check_flash(d.expected_flash_contains, m.flash_text)
        res.status, res.detail = classify(d, baseline, m.total)

        if res.status == "PASS" and res.flash_check not in (None, "ok", "skipped"):
            res.status = "FAIL"
            res.detail = f"{res.detail}; flash {res.flash_check}"

        # Optional negative test.
        if negative_url:
            try:
                neg = measure_with_code(p, negative_url, d.code, headed,
                                        cart_mode=mode)
                neg_baseline = baselines.get(f"{negative_url}#{mode}") or \
                    measure_baseline(p, negative_url, headed, cart_mode=mode)
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
                      limit_per_discount: int = 3
                      ) -> dict[str, tuple[list[str], Optional[str]]]:
    """For each active discount, resolve (positive_urls, negative_url).

    Honors manual overrides (test_product_url / non_matching_product_url).
    Otherwise scrapes the matching category listing on alensa.cz.

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

        for d in active:
            brand_k = _brand_key(d.applies_to.brand)
            # Positive products.
            if d.test_product_url:
                positives = [d.test_product_url]
            else:
                key = (brand_k, d.applies_to.product_type, limit_per_discount)
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
                nkey = (brand_k, d.applies_to.product_type)
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
    args = ap.parse_args()

    cfg = load_config(args.config)
    active = [d for d in cfg.discounts if d.active]
    if not active:
        print("No active discounts in config — nothing to do.")
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
        print(f"[discover] resolving product URLs for {len(active)} discount(s)...")
        per_discount = discover_products(p, active, args.headed)
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
                baselines[key] = measure_baseline(p, url, args.headed,
                                                  diag_out=diag,
                                                  cart_mode=mode)
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
