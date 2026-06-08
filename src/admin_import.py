"""Parse a discount's admin edit-page HTML into our discounts.json schema.

The admin page (Nette form) embeds a hidden input:
    <input name="previousValues" value="{...JSON...}">
containing a complete machine-written dump of every form field. That's
our primary source — far more robust than scraping individual widgets.

We also read the codesActive <select> options, because previousValues
stores codes as internal IDs (e.g. "72692") while the option text holds
the actual coupon string (e.g. "25zcrmaki").

Brand + product_type can't be derived from the category-group IDs in the
form (they're opaque numeric IDs). We approximate them from the
human-readable name / text_info via a known-brand list, and flag for the
operator to confirm.
"""
from __future__ import annotations

import html as _html
import json
import re
from dataclasses import dataclass, field
from typing import Optional

# ref_discounts_list option value -> our discount_type (None = unsupported)
REF_DISCOUNT_TYPE_MAP = {
    "1": "percentage",      # Percentic %
    "2": "fixed_amount",    # Money amount
    "8": "fixed_amount",    # FIX price
    "7": "fixed_amount",    # FIX price frames
    # 4/5 = free delivery, 6 = N-th product, 10 = other — not modelled yet
}

# Known eyewear brands on alensa.cz. Used to pull a brand list out of the
# discount's name / info text. Extend as new brands appear.
KNOWN_BRANDS = [
    "Crullé", "Crulle", "Polaroid", "Tommy Hilfiger", "Boss by Hugo Boss",
    "Hugo by Hugo Boss", "Hugo Boss", "Marc Jacobs", "Moschino",
    "Carolina Herrera", "Carrera", "LeWish", "Marisio", "Kimikado",
    "Chiara Ferragni", "David Beckham", "Dsquared2", "Fossil", "Havaianas",
    "Jimmy Choo", "Kate Spade", "Levi's", "Levi", "Missoni", "Pierre Cardin",
    "Under Armour", "Gelone", "ReNu", "Zeiss", "Hoya", "Nikon",
    "Acuvue", "Biofinity", "Air Optix", "Dailies", "TopVue", "PureVision",
    "SofLens",
]

# Product-type hint words (Czech) appearing in name / info text.
TYPE_HINTS = [
    (("brýle", "brýlové obruby", "obruby", "obrouček", "dioptrické"), "glasses"),
    (("sluneční",), "sunglasses"),
    (("čočky", "kontaktní"), "contact_lenses"),
    (("roztok", "roztoky"), "solutions"),
    (("kapky", "oční kapky"), "eye_drops"),
    (("brýlová skla", "skla", "čočky do brýlí"), "lenses_for_glasses"),
]


@dataclass
class ParsedDiscount:
    discount: dict                      # ready-to-save JSON entry
    warnings: list[str] = field(default_factory=list)
    raw_previous_values: Optional[dict] = None
    category_group_ids: list[str] = field(default_factory=list)


class AdminImportError(ValueError):
    pass


def _extract_previous_values(html_text: str) -> dict:
    """Pull and JSON-decode the previousValues hidden input from the
    discountsEditForm (NOT the interval/delivery sub-forms)."""
    # The value is HTML-entity-encoded JSON. Match the discountsEditForm one.
    m = re.search(
        r'name="previousValues"[^>]*\bvalue="(\{.*?\})"',
        html_text,
        re.DOTALL,
    )
    if not m:
        raise AdminImportError(
            "Couldn't find the previousValues field. Make sure you pasted the "
            "full discount edit-page HTML (the <form id='frm-discountsEditForm'> "
            "section)."
        )
    raw = _html.unescape(m.group(1))
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise AdminImportError(f"previousValues JSON didn't parse: {e}")


def _extract_active_codes(html_text: str) -> list[str]:
    """Read the selected option texts from <select name='codesActive[]'>.
    These are the human-readable coupon strings."""
    sel = re.search(
        r"name=\"codesActive\[\]\"(.*?)</select>",
        html_text,
        re.DOTALL,
    )
    if not sel:
        return []
    block = sel.group(1)
    codes = []
    for opt in re.finditer(
        r"<option[^>]*\bselected[^>]*>(.*?)</option>", block, re.DOTALL
    ):
        txt = _html.unescape(opt.group(1)).strip()
        if txt:
            codes.append(txt)
    # Fallback: if none flagged selected, take all options (single-code case).
    if not codes:
        for opt in re.finditer(r"<option[^>]*>(.*?)</option>", block, re.DOTALL):
            txt = _html.unescape(opt.group(1)).strip()
            if txt:
                codes.append(txt)
    return codes


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", " ", s or "")


def _guess_brands(*texts: str) -> list[str]:
    """Find known brand names mentioned in the given texts."""
    hay = " ".join(_strip_tags(t) for t in texts if t).lower()
    found: list[str] = []
    for brand in KNOWN_BRANDS:
        if brand.lower() in hay and brand not in found:
            found.append(brand)
    # Collapse Crulle->Crullé and Levi->Levi's duplicates, prefer accented form
    if "Crullé" in found and "Crulle" in found:
        found.remove("Crulle")
    if "Levi's" in found and "Levi" in found:
        found.remove("Levi")
    # "Hugo Boss" is ambiguous with the two backend variants; keep as-is.
    return found


def _guess_product_type(*texts: str) -> Optional[str]:
    hay = " ".join(_strip_tags(t) for t in texts if t).lower()
    # lenses_for_glasses is more specific than glasses — check it first.
    ordered = sorted(TYPE_HINTS, key=lambda x: x[1] != "lenses_for_glasses")
    for words, ptype in ordered:
        if any(w in hay for w in words):
            return ptype
    return None


def _to_float(v) -> Optional[float]:
    if v in (None, "", "null"):
        return None
    try:
        f = float(str(v).replace(",", "."))
        return f
    except (TypeError, ValueError):
        return None


def parse_admin_html(html_text: str) -> ParsedDiscount:
    pv = _extract_previous_values(html_text)
    warnings: list[str] = []

    # --- discount type + value ---
    ref = str(pv.get("ref_discounts_list", ""))
    dtype = REF_DISCOUNT_TYPE_MAP.get(ref)
    if dtype is None:
        warnings.append(
            f"Discount type ref={ref!r} isn't a percentage/fixed type we "
            "test yet — defaulting to 'percentage', please verify."
        )
        dtype = "percentage"

    if dtype == "percentage":
        value = _to_float(pv.get("percent")) or 0.0
    else:
        value = _to_float(pv.get("fix_price")) or _to_float(pv.get("amount")) or 0.0
        if value == 0.0:
            warnings.append("Fixed-amount value parsed as 0 — check fix_price/amount.")

    # --- codes ---
    codes = _extract_active_codes(html_text)
    if not codes:
        warnings.append("No active coupon code found in codesActive options.")
        code = ""
    else:
        code = codes[0]
        if len(codes) > 1:
            warnings.append(
                f"Discount has {len(codes)} active codes {codes}; using the "
                f"first ('{code}'). Add the others as separate entries if needed."
            )

    # --- conditions ---
    from_price = _to_float(pv.get("from_price"))
    min_basket = from_price if (from_price and from_price > 0) else None
    usage_limit_raw = pv.get("usage_limit")
    usage_limit = None
    if usage_limit_raw not in (None, "", "0", 0):
        try:
            usage_limit = int(usage_limit_raw)
        except (TypeError, ValueError):
            pass
    combination_forbidden = str(pv.get("combination_forbidden", "0")) == "1"

    # --- expiry ---
    expires_at = None
    iv = pv.get("interval_timestamp_to")
    if iv:
        # admin uses dd/mm/yy or dd.mm.yyyy — try a couple formats
        warnings.append(
            f"interval_timestamp_to={iv!r} found; set expires_at manually "
            "in ISO format if this discount expires."
        )

    # --- brands + product_type (best-effort) ---
    name = pv.get("name", "")
    info = pv.get("text_info", "") or ""
    desc = pv.get("text", "") or ""
    brands = _guess_brands(name, info)
    ptype = _guess_product_type(name, info, desc)
    if not brands:
        warnings.append(
            "Couldn't detect any brand from the name/info text — set "
            "applies_to.brand manually (or leave null for 'any brand')."
        )
    if not ptype:
        warnings.append(
            "Couldn't detect product_type from the text — set "
            "applies_to.product_type manually."
        )

    cat_ids_raw = pv.get("categories_items_groups_1") or []
    cat_ids = [str(x) for x in cat_ids_raw] if isinstance(cat_ids_raw, list) else []
    if cat_ids:
        warnings.append(
            f"Backend category-group IDs {cat_ids} are opaque — they encode "
            "the real brand/type restriction but we can't decode them here. "
            "The brand/type above is guessed from the discount text; verify "
            "against the backend's 'Zařazené kategorie'."
        )

    # --- flash text expectation ---
    # Cart chip shows '<code> (-N%)'. Build a sensible default.
    flash = [code] if code else []
    if dtype == "percentage" and value:
        flash.append(f"-{value:g}%")

    brand_value: object
    if len(brands) > 1:
        brand_value = brands
    elif len(brands) == 1:
        brand_value = brands[0]
    else:
        brand_value = None

    discount = {
        "name": name,
        "code": code,
        "description": _strip_tags(desc).strip(),
        "active": False,  # always import inactive; operator verifies then flips
        "discount_type": dtype,
        "value": value,
        "tolerance_pct": 1.0,
        "test_product_url": None,
        "non_matching_product_url": None,
        "expected_flash_contains": flash,
        "expires_at": expires_at,
        "usage_limit": usage_limit,
        "combination_forbidden": combination_forbidden,
        "min_items": None,
        "applies_to": {
            "brand": brand_value,
            "product_type": ptype,
            "excluded_brands": [],
        },
        "conditions": {
            "min_basket_czk": min_basket,
            "delivery_method": None,
        },
        "notes": (
            f"Imported from admin (id_discount={pv.get('id_discount')}). "
            f"Backend category groups: {cat_ids}. Verify brand/type."
        ),
    }

    return ParsedDiscount(
        discount=discount,
        warnings=warnings,
        raw_previous_values=pv,
        category_group_ids=cat_ids,
    )


if __name__ == "__main__":
    import sys
    txt = open(sys.argv[1], encoding="utf-8").read()
    result = parse_admin_html(txt)
    print(json.dumps(result.discount, indent=2, ensure_ascii=False))
    print("\n--- warnings ---")
    for w in result.warnings:
        print(f"  ! {w}")
