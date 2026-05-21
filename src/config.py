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
    None,
}
ALLOWED_DELIVERY_METHODS = {None}  # extend later, e.g. "ppl", "zasilkovna"


class ConfigError(ValueError):
    pass


@dataclass
class AppliesTo:
    brand: Optional[str] = None
    product_type: Optional[str] = None


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
    applies = AppliesTo(
        brand=applies_raw.get("brand"),
        product_type=applies_raw.get("product_type"),
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

    return Discount(
        name=str(name),
        code=str(code),
        discount_type=dtype,
        value=float(value),
        active=bool(d.get("active", True)),
        tolerance_pct=float(tol),
        test_product_url=d.get("test_product_url"),
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
