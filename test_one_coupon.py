"""Calibration / smoke test: apply one coupon to one product and report.

This is NOT the final test runner. It's the minimal end-to-end script we use
to confirm the alensa_cz.py functions actually work on the live site.

    python test_one_coupon.py --product https://www.alensa.cz/roztok-gelone-360-ml \
                              --code GEL26CARE
    python test_one_coupon.py --headed   # show the browser
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

from src.sites import alensa_cz


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--product", required=True, help="Product URL to add to cart")
    ap.add_argument("--code", required=True, help="Coupon code to test")
    ap.add_argument("--headed", action="store_true", help="Show the browser")
    args = ap.parse_args()

    reports = Path("reports")
    reports.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed, slow_mo=150)
        context = browser.new_context(locale="cs-CZ",
                                      viewport={"width": 1366, "height": 900})
        page = context.new_page()

        print("1. open homepage + accept cookies")
        safe = alensa_cz.open_homepage(page)

        print(f"2. add to cart: {args.product}")
        alensa_cz.add_to_cart(safe, args.product)

        print("3. go to cart")
        alensa_cz.go_to_cart(safe)
        page.screenshot(path=str(reports / f"run_{ts}_cart_before.png"), full_page=True)
        total_before = alensa_cz.read_cart_total(safe)
        print(f"   subtotal before coupon: {total_before} CZK")

        print(f"4. apply coupon: {args.code}")
        result = alensa_cz.apply_coupon(safe, args.code)
        page.screenshot(path=str(reports / f"run_{ts}_cart_after.png"), full_page=True)

        print("5. proceed to checkout (will NOT pay)")
        try:
            alensa_cz.proceed_to_checkout(safe)
            print(f"   reached: {page.url}")
            page.screenshot(path=str(reports / f"run_{ts}_checkout.png"), full_page=True)
        except Exception as e:
            print(f"   proceed_to_checkout failed: {e}")

        # Save structured report.
        out = {
            "timestamp": ts,
            "product_url": args.product,
            "code": result.code,
            "applied": result.applied,
            "total_before_czk": result.total_before,
            "total_after_czk": result.total_after,
            "discount_czk": result.discount_czk,
            "discount_pct": result.discount_pct,
            "flash_message": result.flash_message,
            "final_url": page.url,
        }
        report_path = reports / f"run_{ts}.json"
        report_path.write_text(json.dumps(out, indent=2, ensure_ascii=False),
                               encoding="utf-8")
        print()
        print(f"== RESULT for code {result.code} ==")
        print(f"  applied:        {result.applied}")
        print(f"  total before:   {result.total_before} CZK")
        print(f"  total after:    {result.total_after} CZK")
        print(f"  discount:       {result.discount_czk} CZK ({result.discount_pct}%)")
        if result.flash_message:
            print(f"  site message:   {result.flash_message}")
        print(f"  report:         {report_path}")

        context.close()
        browser.close()


if __name__ == "__main__":
    main()
