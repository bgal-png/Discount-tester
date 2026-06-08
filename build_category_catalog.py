"""Build the category catalog from saved admin grid HTML pages.

Usage:
    1. In the admin /global-catalog/categories grid, set 1000 items/page.
    2. Save each page's HTML into the category_pages/ folder
       (any filename ending .html — e.g. page_01.html, page_02.html, ...).
    3. Run:  python build_category_catalog.py

Outputs:
    config/category_catalog.json  -> {id: {category, name, description, priority}}
                                      consumed by the discount importer
    config/category_catalog.xlsx  -> human-viewable sheet (ID, Kategorie,
                                      Jméno, Popis, Priorita), sorted by ID

Re-running is safe: it re-reads every file and rebuilds from scratch, so
duplicate IDs across pages collapse to one row.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font

from src.category_catalog import parse_category_grid, items_to_map

ROOT = Path(__file__).parent
PAGES_DIR = ROOT / "category_pages"
JSON_OUT = ROOT / "config" / "category_catalog.json"
XLSX_OUT = ROOT / "config" / "category_catalog.xlsx"


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")

    if not PAGES_DIR.is_dir():
        print(f"No {PAGES_DIR} folder. Create it and drop saved grid HTML "
              "pages inside (*.html).")
        return

    html_files = sorted(PAGES_DIR.glob("*.html"))
    if not html_files:
        print(f"No .html files in {PAGES_DIR}. Save the grid pages there first.")
        return

    catalog: dict[str, dict] = {}
    per_file = []
    for fp in html_files:
        items = parse_category_grid(fp.read_text(encoding="utf-8"))
        catalog.update(items_to_map(items))
        per_file.append((fp.name, len(items)))

    # Write JSON (sorted by numeric id for stable diffs).
    ordered = {k: catalog[k] for k in sorted(catalog, key=lambda x: int(x))}
    JSON_OUT.parent.mkdir(parents=True, exist_ok=True)
    JSON_OUT.write_text(
        json.dumps(ordered, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Write Excel.
    wb = Workbook()
    ws = wb.active
    ws.title = "Categories"
    ws.append(["ID", "Kategorie", "Jméno", "Popis", "Priorita"])
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for cid, row in ordered.items():
        ws.append([
            int(cid), row.get("category", ""), row.get("name", ""),
            row.get("description", ""), row.get("priority", ""),
        ])
    # Reasonable column widths.
    widths = {"A": 10, "B": 34, "C": 40, "D": 30, "E": 10}
    for col, w in widths.items():
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    wb.save(XLSX_OUT)

    print("Per-file row counts:")
    for name, n in per_file:
        print(f"  {name}: {n}")
    print(f"\nTotal unique category items: {len(ordered)}")
    print(f"Wrote:\n  {JSON_OUT}\n  {XLSX_OUT}")

    # Quick distribution of category types for sanity.
    from collections import Counter
    by_cat = Counter(r["category"] for r in ordered.values())
    print("\nTop category types by count:")
    for cat, n in by_cat.most_common(12):
        print(f"  {n:>6}  {cat}")


if __name__ == "__main__":
    main()
