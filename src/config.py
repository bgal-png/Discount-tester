"""Load and validate discounts.json.

Designed to fail loudly on typos before any browser opens.
Extend ALLOWED_* sets as we add new discount kinds.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

ALLOWED_DISCOUNT_TYPES = {"percentage", "fixed_amount"}
ALLOWED_PRODUCT_TYPES = {
    "glasses",
    "sunglasses",
    "contact_lenses",
    "solutions",
    "eye_drops",
    "accessories",
    "lenses_for_glasses",   # spectacle lenses, ordered with a frame
    None,
}
ALLOWED_DELIVERY_METHODS = {None}  # extend later, e.g. "ppl", "zasilkovna"


class ConfigError(ValueError):
    pass


@dataclass
class AppliesTo:
    # Single brand or list of brands. The runner treats a list as "any of
    # these brands qualifies" (matches OR-style backend Group selection).
    brand: Optional[str | list[str]] = None
    product_type: Optional[str] = None
    # Brands explicitly excluded (e.g. "Spectacle lens brand - Zeiss/Hoya/Nikon"
    # in the backend's Vyřazené kategorie). Used by the negative test picker
    # and informational for humans reviewing the config.
    excluded_brands: list[str] = field(default_factory=list)


@dataclass
class Conditions:
    min_basket_czk: Optional[float] = None
    delivery_method: Optional[str] = None


@dataclass
class Discount:
    name: str
    code: str
    discount_type: str
    value: float
    active: bool = True
    tolerance_pct: float = 1.0  # absolute percentage-point tolerance (e.g. 1.0 = ±1pp)
    test_product_url: Optional[str] = None
    non_matching_product_url: Optional[str] = None  # negative test (optional)
    expected_flash_contains: list[str] = field(default_factory=list)  # all must appear
    description: str = ""                  # internal note from backend
    expires_at: Optional[str] = None       # ISO date "YYYY-MM-DD"; runner skips past
    combination_forbidden: bool = False    # informational
    min_items: Optional[int] = None        # informational
    usage_limit: Optional[int] = None      # informational
    applies_to: AppliesTo = field(default_factory=AppliesTo)
    conditions: Conditions = field(default_factory=Conditions)
    notes: str = ""


@dataclass
class DiscountConfig:
    site: str
    currency: str
    discounts: list[Discount]


def _require(d: dict, key: str, where: str):
    if key not in d:
        raise ConfigError(f"Missing required field '{key}' in {where}")
    return d[key]


def _parse_discount(d: dict, idx: int) -> Discount:
    where = f"discounts[{idx}]"
    name = _require(d, "name", where)
    code = _require(d, "code", where)
    dtype = _require(d, "discount_type", where)
    value = _require(d, "value", where)

    if dtype not in ALLOWED_DISCOUNT_TYPES:
        raise ConfigError(
            f"{where}: discount_type '{dtype}' not in {ALLOWED_DISCOUNT_TYPES}"
        )
    if not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f"{where}: value must be a positive number, got {value!r}")
    if dtype == "percentage" and value > 100:
        raise ConfigError(f"{where}: percentage value must be <= 100")

    applies_raw = d.get("applies_to") or {}
    brand = applies_raw.get("brand")
    if brand is not None and not isinstance(brand, (str, list)):
        raise ConfigError(f"{where}: applies_to.brand must be string, list, or null")
    if isinstance(brand, list) and not all(isinstance(b, str) for b in brand):
        raise ConfigError(f"{where}: applies_to.brand list must contain only strings")
    excluded_brands_raw = applies_raw.get("excluded_brands", []) or []
    if not isinstance(excluded_brands_raw, list) or \
            not all(isinstance(b, str) for b in excluded_brands_raw):
        raise ConfigError(
            f"{where}: applies_to.excluded_brands must be a list of strings"
        )
    applies = AppliesTo(
        brand=brand,
        product_type=applies_raw.get("product_type"),
        excluded_brands=excluded_brands_raw,
    )
    if applies.product_type not in ALLOWED_PRODUCT_TYPES:
        raise ConfigError(
            f"{where}: product_type '{applies.product_type}' not in {ALLOWED_PRODUCT_TYPES}"
        )

    cond_raw = d.get("conditions") or {}
    cond = Conditions(
        min_basket_czk=cond_raw.get("min_basket_czk"),
        delivery_method=cond_raw.get("delivery_method"),
    )
    if cond.delivery_method not in ALLOWED_DELIVERY_METHODS:
        raise ConfigError(
            f"{where}: delivery_method '{cond.delivery_method}' not allowed yet"
        )

    tol = d.get("tolerance_pct", 1.0)
    if not isinstance(tol, (int, float)) or tol < 0:
        raise ConfigError(f"{where}: tolerance_pct must be a non-negative number")

    flash_raw = d.get("expected_flash_contains", [])
    if isinstance(flash_raw, str):
        flash_raw = [flash_raw]  # convenience: allow a single string
    if not isinstance(flash_raw, list) or not all(isinstance(x, str) for x in flash_raw):
        raise ConfigError(f"{where}: expected_flash_contains must be a string or list of strings")

    expires_at = d.get("expires_at")
    if expires_at is not None:
        import datetime
        try:
            datetime.date.fromisoformat(expires_at)
        except (TypeError, ValueError):
            raise ConfigError(f"{where}: expires_at must be ISO date 'YYYY-MM-DD', got {expires_at!r}")

    min_items = d.get("min_items")
    if min_items is not None and (not isinstance(min_items, int) or min_items < 0):
        raise ConfigError(f"{where}: min_items must be a non-negative integer")

    usage_limit = d.get("usage_limit")
    if usage_limit is not None and (not isinstance(usage_limit, int) or usage_limit < 0):
        raise ConfigError(f"{where}: usage_limit must be a non-negative integer")

    return Discount(
        name=str(name),
        code=str(code),
        discount_type=dtype,
        value=float(value),
        active=bool(d.get("active", True)),
        tolerance_pct=float(tol),
        test_product_url=d.get("test_product_url"),
        non_matching_product_url=d.get("non_matching_product_url"),
        expected_flash_contains=flash_raw,
        description=str(d.get("description", "")),
        expires_at=expires_at,
        combination_forbidden=bool(d.get("combination_forbidden", False)),
        min_items=min_items,
        usage_limit=usage_limit,
        applies_to=applies,
        conditions=cond,
        notes=str(d.get("notes", "")),
    )


def load_config(path: str | Path) -> DiscountConfig:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")

    try:
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"Invalid JSON in {path}: {e}") from e

    site = _require(raw, "site", "root")
    currency = _require(raw, "currency", "root")
    discounts_raw = _require(raw, "discounts", "root")
    if not isinstance(discounts_raw, list):
        raise ConfigError("'discounts' must be a list")

    discounts = [_parse_discount(d, i) for i, d in enumerate(discounts_raw)]

    codes = [d.code for d in discounts]
    duplicates = {c for c in codes if codes.count(c) > 1}
    if duplicates:
        raise ConfigError(f"Duplicate discount codes: {duplicates}")

    return DiscountConfig(site=site, currency=currency, discounts=discounts)


if __name__ == "__main__":
    import sys

    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "config/discounts.json"
    cfg = load_config(cfg_path)
    print(f"Loaded {len(cfg.discounts)} discounts for {cfg.site} ({cfg.currency})")
    for d in cfg.discounts:
        flag = "ACTIVE" if d.active else "inactive"
        print(f"  [{flag}] {d.code}: {d.name} ({d.discount_type} {d.value})")
