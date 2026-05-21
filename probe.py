"""Probe a single page: dump buttons, inputs, headings, prices.

Usage:
    python probe.py <url> [--name product] [--headless]

Writes:
    reports/probe_<name>.png       screenshot
    reports/probe_<name>.html      full HTML snapshot
    reports/probe_<name>.txt       human-readable summary
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from playwright.sync_api import sync_playwright, Page

from src.sites import alensa_cz


PRICE_RE = re.compile(r"(\d[\d\s ]*[,.]?\d*)\s*(Kč|CZK|€|EUR)", re.I)


def dump_buttons(page: Page) -> list[str]:
    lines = ["== BUTTONS =="]
    for b in page.locator("button, a[role=button], a.btn, a.button, input[type=submit], input[type=button]").all():
        try:
            if not b.is_visible():
                continue
            text = (b.inner_text() or b.get_attribute("value") or "").strip()
            text = re.sub(r"\s+", " ", text)[:80]
            if not text:
                continue
            attrs = []
            for a in ("id", "name", "class", "data-testid", "aria-label"):
                v = b.get_attribute(a)
                if v:
                    attrs.append(f"{a}={v[:60]!r}")
            lines.append(f"  [{text}]   {' '.join(attrs)}")
        except Exception:
            continue
    return lines


def dump_inputs(page: Page) -> list[str]:
    lines = ["== INPUTS / SELECTS =="]
    for el in page.locator("input:not([type=hidden]), select, textarea").all():
        try:
            if not el.is_visible():
                continue
            tag = el.evaluate("e => e.tagName.toLowerCase()")
            attrs = []
            for a in ("type", "id", "name", "placeholder", "aria-label", "data-testid"):
                v = el.get_attribute(a)
                if v:
                    attrs.append(f"{a}={v[:60]!r}")
            lines.append(f"  <{tag}>  {' '.join(attrs)}")
        except Exception:
            continue
    return lines


def dump_headings(page: Page) -> list[str]:
    lines = ["== HEADINGS =="]
    for h in page.locator("h1, h2").all():
        try:
            if not h.is_visible():
                continue
            tag = h.evaluate("e => e.tagName.toLowerCase()")
            text = re.sub(r"\s+", " ", (h.inner_text() or "").strip())[:120]
            if text:
                lines.append(f"  <{tag}> {text}")
        except Exception:
            continue
    return lines


def dump_prices(page: Page) -> list[str]:
    lines = ["== PRICES ON PAGE (top 20) =="]
    body_text = page.inner_text("body")
    found = []
    for m in PRICE_RE.finditer(body_text):
        found.append(m.group(0).strip())
    seen = []
    for p in found:
        if p not in seen:
            seen.append(p)
        if len(seen) >= 20:
            break
    for p in seen:
        lines.append(f"  {p}")
    return lines


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--name", default="page")
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()

    reports = Path("reports")
    reports.mkdir(exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        context = browser.new_context(locale="cs-CZ",
                                      viewport={"width": 1366, "height": 900})
        page = context.new_page()

        # Land on homepage first so cookie banner gets handled with allowed-host
        # safety, then navigate to the target URL.
        alensa_cz.open_homepage(page)
        page.goto(args.url, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)

        png = reports / f"probe_{args.name}.png"
        html = reports / f"probe_{args.name}.html"
        txt = reports / f"probe_{args.name}.txt"

        page.screenshot(path=str(png), full_page=True)
        html.write_text(page.content(), encoding="utf-8")

        summary = [
            f"URL: {page.url}",
            f"TITLE: {page.title()}",
            "",
            *dump_headings(page),
            "",
            *dump_buttons(page),
            "",
            *dump_inputs(page),
            "",
            *dump_prices(page),
        ]
        txt.write_text("\n".join(summary), encoding="utf-8")
        print(f"Wrote: {png.name}, {html.name}, {txt.name}")

        context.close()
        browser.close()


if __name__ == "__main__":
    main()
