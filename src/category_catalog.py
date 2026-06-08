"""Parse the admin 'Categories items' datagrid into an ID -> (category, name)
catalog.

The admin grid at /global-catalog/categories lists ~51k category items,
each a row with columns:
    ID | Kategorie (category type) | Jméno (item name) | Popis | Priorita

A discount's `categories_items_groups_N` field holds IDs into THIS table.
Decoding them gives the real brand/type restriction:
    ID 4816 -> Kategorie="Manufacturer", Jméno="Crullé"  => brand Crullé
    ID 133  -> Kategorie="Items type",   Jméno="Glasses" => product glasses

Workflow for the full 51k:
    1. In the admin, set the grid to 1000 items/page.
    2. Save each page's HTML into category_pages/  (page_01.html, ...).
    3. Run:  python build_category_catalog.py
       -> writes config/category_catalog.json  (ID -> {category, name})
              config/category_catalog.xlsx  (human-viewable)
"""
from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass


@dataclass
class CategoryItem:
    id: int
    category: str   # the "Kategorie" column (type, e.g. "Manufacturer")
    name: str       # the "Jméno" column (value, e.g. "Crullé")
    description: str = ""
    priority: str = ""


_ROW_RE = re.compile(
    r'<tr[^>]*id="categoriesItemsGrid___id___\d+".*?</tr>',
    re.DOTALL,
)
_CELL_RE = re.compile(
    r'data-datagrid-label="([^"]+)"\s*>\s*'
    r'<div class="datagrid-cell-content">(.*?)</div>',
    re.DOTALL,
)


def _clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)        # strip any inner tags
    return _html.unescape(s).strip()


def parse_category_grid(html_text: str) -> list[CategoryItem]:
    """Extract every category-item row from one or more datagrid HTML chunks.

    Robust to extra whitespace and to multiple tables concatenated together.
    Rows missing an ID or both category+name are skipped.
    """
    items: list[CategoryItem] = []
    for row_html in _ROW_RE.findall(html_text):
        cells = {label: _clean(val) for label, val in _CELL_RE.findall(row_html)}
        # Column labels are Czech in the admin: ID / Kategorie / Jméno / Popis / Priorita
        raw_id = cells.get("ID", "")
        if not raw_id.isdigit():
            continue
        category = cells.get("Kategorie", "")
        name = cells.get("Jméno", "")
        if not category and not name:
            continue
        items.append(CategoryItem(
            id=int(raw_id),
            category=category,
            name=name,
            description=cells.get("Popis", ""),
            priority=cells.get("Priorita", ""),
        ))
    return items


def items_to_map(items: list[CategoryItem]) -> dict[str, dict]:
    """ID(str) -> {category, name, description, priority}. Later items win on
    duplicate IDs (so re-pasting a page just refreshes it)."""
    out: dict[str, dict] = {}
    for it in items:
        out[str(it.id)] = {
            "category": it.category,
            "name": it.name,
            "description": it.description,
            "priority": it.priority,
        }
    return out


if __name__ == "__main__":
    import sys, json
    txt = open(sys.argv[1], encoding="utf-8").read()
    parsed = parse_category_grid(txt)
    print(f"Parsed {len(parsed)} category items")
    for it in parsed[:15]:
        print(f"  {it.id:>6}  {it.category:30s}  {it.name}")
