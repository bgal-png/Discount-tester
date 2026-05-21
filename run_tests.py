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
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright, Playwright, BrowserContext

from src.config import load_config, Discount
from src.sites import alensa_cz
from src.safety import SafetyViolation


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


def new_context(p: Playwright, headed: bool) -> BrowserContext:
    browser = p.chromium.launch(headless=not headed, slow_mo=100 if headed else 0)
    return browser.new_context(locale="cs-CZ", viewport={"width": 1366, "height": 900})


def measure_baseline(p: Playwright, product_url: str, headed: bool) -> float:
    """Add product to a clean cart, return the subtotal."""
    ctx = new_context(p, headed)
    try:
        page = ctx.new_page()
        safe = alensa_cz.open_homepage(page)
        alensa_cz.add_to_cart(safe, product_url)
        alensa_cz.go_to_cart(safe)
        return alensa_cz.read_cart_total(safe)
    finally:
        ctx.close()


def measure_with_code(p: Playwright, product_url: str, code: str,
                      headed: bool) -> MeasureWithCode:
    """Apply the code via homepage URL param, then add product, then on the
    cart page read both the total and the persistent applied-coupon chip."""
    ctx = new_context(p, headed)
    try:
        page = ctx.new_page()
        safe = alensa_cz.open_homepage(page, dc_code=code)
        alensa_cz.add_to_cart(safe, product_url)
        alensa_cz.go_to_cart(safe)
        total = alensa_cz.read_cart_total(safe)
        chip = alensa_cz.read_applied_coupon_info(safe)
        return MeasureWithCode(total=total, flash_text=chip)
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


def run_one(p: Playwright, d: Discount, baseline: float, headed: bool
            ) -> DiscountTestResult:
    res = DiscountTestResult(
        name=d.name,
        code=d.code,
        product_url=d.test_product_url or "",
        expected_type=d.discount_type,
        expected_value=d.value,
        tolerance_pct=d.tolerance_pct,
        baseline_czk=baseline,
    )
    t0 = time.time()
    try:
        m = measure_with_code(p, d.test_product_url, d.code, headed)
        res.observed_total_czk = m.total
        res.observed_discount_czk = round(baseline - m.total, 2)
        if baseline:
            res.observed_discount_pct = round(100 * res.observed_discount_czk / baseline, 2)
        res.flash_text = m.flash_text
        res.flash_check = check_flash(d.expected_flash_contains, m.flash_text)
        res.status, res.detail = classify(d, baseline, m.total)

        # If the primary test passed but flash text is missing/wrong, downgrade.
        if res.status == "PASS" and res.flash_check not in (None, "ok", "skipped"):
            res.status = "FAIL"
            res.detail = f"{res.detail}; flash {res.flash_check}"

        # Optional negative test.
        if d.non_matching_product_url:
            try:
                neg = measure_with_code(p, d.non_matching_product_url, d.code, headed)
                # On non-matching product, expect total == baseline of that product
                # (i.e. no discount). We don't have its baseline — but if the
                # subtotal decreased noticeably we know the code leaked.
                # Conservative check: any drop > 0.5% counts as leak.
                neg_baseline = measure_baseline(p, d.non_matching_product_url, headed)
                leak = neg_baseline - neg.total
                leak_pct = 100 * leak / neg_baseline if neg_baseline else 0
                if leak_pct > 0.5:
                    res.negative_status = "LEAKED"
                    res.negative_detail = (
                        f"{d.non_matching_product_url} dropped {leak} CZK "
                        f"({leak_pct:.1f}%) when code applied; restriction not enforced"
                    )
                    res.status = "FAIL"
                    res.detail = f"{res.detail}; NEGATIVE: {res.negative_detail}"
                else:
                    res.negative_status = "PASS_NOT_APPLIED"
                    res.negative_detail = (
                        f"{d.non_matching_product_url}: no discount applied "
                        f"(baseline {neg_baseline} vs with-code {neg.total})"
                    )
            except Exception as e:
                res.negative_status = "ERROR"
                res.negative_detail = f"{type(e).__name__}: {e}"
        else:
            res.negative_status = "SKIPPED"

    except SafetyViolation as e:
        res.status, res.detail = "ERROR", f"SAFETY: {e}"
    except Exception as e:
        res.status, res.detail = "ERROR", f"{type(e).__name__}: {e}"
    res.duration_s = round(time.time() - t0, 1)
    return res


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
    args = ap.parse_args()

    cfg = load_config(args.config)
    active = [d for d in cfg.discounts if d.active]
    if not active:
        print("No active discounts in config — nothing to do.")
        return

    missing = [d.code for d in active if not d.test_product_url]
    if missing:
        print(f"ERROR: these active discounts have no test_product_url: {missing}")
        return

    print(f"Loaded {len(active)} active discount(s) for {cfg.site}")

    reports = Path("reports")
    reports.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    results: list[DiscountTestResult] = []

    with sync_playwright() as p:
        # Baseline per unique product (avoids re-measuring for shared products).
        baselines: dict[str, float] = {}
        unique_products = sorted({d.test_product_url for d in active})
        for url in unique_products:
            print(f"[baseline] {url}")
            try:
                baselines[url] = measure_baseline(p, url, args.headed)
                print(f"           {baselines[url]} CZK")
            except Exception as e:
                print(f"           ERROR measuring baseline: {e}")
                baselines[url] = float("nan")

        today = date.today()
        # Each active discount.
        for d in active:
            if is_expired(d, today):
                print(f"[skip] {d.code} expired {d.expires_at}")
                results.append(DiscountTestResult(
                    name=d.name, code=d.code, product_url=d.test_product_url,
                    expected_type=d.discount_type, expected_value=d.value,
                    tolerance_pct=d.tolerance_pct,
                    status="SKIPPED_EXPIRED",
                    detail=f"expired {d.expires_at}",
                ))
                continue
            print(f"[test] {d.code}  ({d.name})")
            baseline = baselines[d.test_product_url]
            if baseline != baseline:  # NaN
                results.append(DiscountTestResult(
                    name=d.name, code=d.code, product_url=d.test_product_url,
                    expected_type=d.discount_type, expected_value=d.value,
                    tolerance_pct=d.tolerance_pct,
                    status="ERROR", detail="baseline measurement failed",
                ))
                continue
            results.append(run_one(p, d, baseline, args.headed))
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


if __name__ == "__main__":
    main()
