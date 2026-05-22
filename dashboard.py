"""Streamlit dashboard: configure discounts, run tests, browse reports.

Launch via the double-clickable start_dashboard.bat, or:
    streamlit run dashboard.py

Designed for local use — tests run on this machine via Playwright.
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st


from src.config import load_config, ConfigError  # noqa: E402

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config" / "discounts.json"
REPORTS_DIR = ROOT / "reports"

GITHUB_REPO = "bgal-png/Discount-tester"
GITHUB_EDIT_URL = f"https://github.com/{GITHUB_REPO}/edit/main/config/discounts.json"

BLANK_DISCOUNT_TEMPLATE = {
    "name": "REPLACE — display name",
    "code": "REPLACE_CODE",
    "description": "internal note from backend",
    "active": False,
    "discount_type": "percentage",
    "value": 10,
    "tolerance_pct": 1.0,
    "test_product_url": "https://www.alensa.cz/REPLACE",
    "non_matching_product_url": None,
    "expected_flash_contains": ["REPLACE_CODE", "-10%"],
    "expires_at": None,
    "usage_limit": None,
    "combination_forbidden": False,
    "min_items": None,
    "applies_to": {"brand": None, "product_type": None},
    "conditions": {"min_basket_czk": None, "delivery_method": None},
    "notes": "",
}

STATUS_COLOR = {
    "PASS": "🟢",
    "FAIL": "🔴",
    "NOT_APPLIED": "🟠",
    "ERROR": "⚪",
    "SKIPPED_EXPIRED": "⚫",
}


# ---------- helpers ----------

def list_reports() -> list[Path]:
    if not REPORTS_DIR.exists():
        return []
    return sorted(REPORTS_DIR.glob("run_*.json"), reverse=True)


def load_report(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def render_diagnostics(results: list[dict], key_prefix: str = "diag") -> None:
    """If any result has diagnostics, show them inline (screenshot + URL/title).

    key_prefix must be unique per call site (Streamlit element keys are
    globally namespaced within a single script run).
    """
    diag_results = [r for r in results if r.get("diagnostics")]
    if not diag_results:
        return
    st.subheader("🔍 Diagnostics for failures")
    st.caption(
        "When a test fails, we capture the page state at that moment. "
        "Most useful signal for figuring out what went wrong — especially "
        "on Streamlit Cloud where the environment differs from local."
    )
    for r in diag_results:
        d = r["diagnostics"]
        with st.expander(f"❌ {r['code']} — {r.get('detail', 'error')[:120]}", expanded=True):
            cols = st.columns([2, 3])
            with cols[0]:
                st.write(f"**Landed at:** `{d.get('url', '')}`")
                st.write(f"**Page title:** `{d.get('title', '')}`")
                if d.get("screenshot"):
                    shot_path = Path(d["screenshot"])
                    if shot_path.exists():
                        with open(shot_path, "rb") as fh:
                            st.download_button(
                                "⬇ Download screenshot",
                                data=fh.read(),
                                file_name=shot_path.name,
                                mime="image/png",
                                key=f"{key_prefix}_dl_{r['code']}_{shot_path.name}",
                            )
            with cols[1]:
                if d.get("screenshot"):
                    shot_path = Path(d["screenshot"])
                    if shot_path.exists():
                        st.image(str(shot_path), use_container_width=True)


def _product_slug(url: str) -> str:
    """Take a product URL and return just the slug, e.g.
    'https://www.alensa.cz/roztok-gelone-360-ml' -> 'roztok-gelone-360-ml'.
    """
    if not url:
        return ""
    return url.rstrip("/").rsplit("/", 1)[-1]


def results_dataframe(report: dict) -> pd.DataFrame:
    rows = []
    for r in report.get("results", []):
        rows.append({
            "": STATUS_COLOR.get(r["status"], ""),
            "status": r["status"],
            "code": r["code"],
            "product": _product_slug(r.get("product_url", "")),
            "expected": f"{r['expected_value']}{'%' if r['expected_type'] == 'percentage' else ' CZK'}",
            "observed %": r.get("observed_discount_pct"),
            "baseline CZK": r.get("baseline_czk"),
            "after CZK": r.get("observed_total_czk"),
            "saved CZK": r.get("observed_discount_czk"),
            "flash_check": r.get("flash_check"),
            "negative": r.get("negative_status"),
            "duration s": r.get("duration_s"),
            "detail": r.get("detail", ""),
            "name": r["name"],
        })
    return pd.DataFrame(rows)


# ---------- Human-friendly report renderer ----------

def _humanize_run_timestamp(ts: str) -> str:
    """20260522_134812 -> 22 May 2026, 13:48"""
    try:
        dt = datetime.strptime(ts, "%Y%m%d_%H%M%S")
        return dt.strftime("%d %b %Y, %H:%M")
    except (ValueError, TypeError):
        return ts


def _discount_overall_status(rows: list[dict]) -> str:
    """Return 'pass' / 'mixed' / 'fail' / 'error' / 'skipped' summarising
    all (discount, product) rows for one discount."""
    statuses = {r["status"] for r in rows}
    if statuses == {"PASS"}:
        return "pass"
    if statuses == {"SKIPPED_EXPIRED"}:
        return "skipped"
    if "PASS" in statuses and (statuses & {"FAIL", "NOT_APPLIED", "ERROR"}):
        return "mixed"
    if statuses & {"FAIL", "NOT_APPLIED"}:
        return "fail"
    return "error"


def _format_value(r: dict) -> str:
    """e.g. '20%' or '100 CZK off'"""
    if r["expected_type"] == "percentage":
        return f"{r['expected_value']:g}%"
    return f"{r['expected_value']:g} CZK off"


def _negative_plain(neg_status: str | None) -> str:
    return {
        "PASS_NOT_APPLIED": "✅ Restriction verified — code doesn't apply outside its scope",
        "LEAKED":           "⚠ LEAK — code applied where it shouldn't have",
        "ERROR":            "⚠ Couldn't verify restriction (test errored)",
        "SKIPPED":          "",  # nothing meaningful to say
        None:               "",
    }.get(neg_status, str(neg_status))


def _per_product_table(rows: list[dict]) -> pd.DataFrame:
    """A compact, product-level table for one discount's section."""
    out = []
    for r in rows:
        status = r["status"]
        icon = STATUS_COLOR.get(status, "")
        observed_pct = r.get("observed_discount_pct")
        saved = r.get("observed_discount_czk")
        baseline = r.get("baseline_czk")
        after = r.get("observed_total_czk")
        out.append({
            "": icon,
            "product": _product_slug(r.get("product_url", "")) or "—",
            "before": f"{baseline:g} CZK" if baseline else "—",
            "after":  f"{after:g} CZK" if after is not None else "—",
            "saved":  f"{saved:g} CZK" if saved else "—",
            "discount applied": f"{observed_pct:g}%" if observed_pct is not None else "—",
        })
    return pd.DataFrame(out)


def render_human_report(report: dict, key_prefix: str = "h") -> None:
    """Plain-language view of a run report. Groups by discount, shows
    problems first, hides the raw table under an Advanced expander."""
    results = report.get("results", [])
    if not results:
        st.info("No results in this report.")
        return

    # Group by discount code (preserve original order).
    by_code: dict[str, list[dict]] = {}
    for r in results:
        by_code.setdefault(r["code"], []).append(r)

    # Top-level banner.
    summary = report.get("summary", {})
    pass_count = summary.get("PASS", 0)
    issue_count = (summary.get("FAIL", 0) + summary.get("NOT_APPLIED", 0)
                   + summary.get("ERROR", 0))
    skipped = summary.get("SKIPPED_EXPIRED", 0)
    total = pass_count + issue_count + skipped

    ts_human = _humanize_run_timestamp(report.get("timestamp", ""))
    n_discounts = len(by_code)

    if issue_count == 0 and pass_count > 0:
        st.success(
            f"### ✅ All {n_discounts} discount{'' if n_discounts == 1 else 's'} working\n"
            f"{pass_count} product test{'' if pass_count == 1 else 's'} passed"
            f"{f', {skipped} expired/skipped' if skipped else ''}. Run on **{ts_human}**."
        )
    elif issue_count > 0:
        st.error(
            f"### ⚠ {issue_count} test{'' if issue_count == 1 else 's'} need attention\n"
            f"{pass_count} passed, {issue_count} failed/errored"
            f"{f', {skipped} expired/skipped' if skipped else ''}. "
            f"Run on **{ts_human}**."
        )
    else:
        st.info(f"### {total} test(s) — see breakdown below. Run on **{ts_human}**.")

    # Order discounts so problems show first, then mixed, then passes.
    order_key = {"fail": 0, "error": 1, "mixed": 2, "pass": 3, "skipped": 4}
    ordered_codes = sorted(
        by_code.keys(),
        key=lambda c: (order_key.get(_discount_overall_status(by_code[c]), 9), c),
    )

    for code in ordered_codes:
        rows = by_code[code]
        first = rows[0]
        overall = _discount_overall_status(rows)
        name = first.get("name", code)
        value_label = _format_value(first)

        pass_n = sum(1 for r in rows if r["status"] == "PASS")
        fail_n = sum(1 for r in rows if r["status"] in ("FAIL", "NOT_APPLIED"))
        err_n  = sum(1 for r in rows if r["status"] == "ERROR")

        # Header icon + one-line summary.
        if overall == "pass":
            icon = "✅"
            sub = f"Working — **{value_label}** applied on all {pass_n} product{'' if pass_n == 1 else 's'} tested."
        elif overall == "skipped":
            icon = "⚫"
            sub = f"Skipped — {first.get('detail', 'expired or inactive')}."
        elif overall == "error":
            icon = "🚫"
            sub = f"Test errored on all {err_n} product{'' if err_n == 1 else 's'} — site or runner couldn't complete."
        elif overall == "mixed":
            icon = "⚠"
            sub = f"Partial — {pass_n} passed, {fail_n + err_n} had problems out of {len(rows)} products."
        else:  # "fail"
            icon = "⚠"
            sub = f"Not working — **{value_label}** expected, but observed differently."

        # Expander expanded only for problems by default.
        with st.expander(
            f"{icon}  **{code}**  —  {name}",
            expanded=overall in ("fail", "error", "mixed"),
        ):
            st.markdown(sub)

            # Per-product mini-table.
            if any(r.get("baseline_czk") is not None for r in rows):
                st.dataframe(
                    _per_product_table(rows),
                    use_container_width=True,
                    hide_index=True,
                )

            # Per-line cart breakdown (when the test added a multi-item
            # bundle like frame + lenses). Helps confirm WHICH line the
            # discount actually hit.
            for r in rows:
                lines = r.get("line_items") or []
                if len(lines) <= 1:
                    continue
                product_label = (_product_slug(r.get("product_url", "")) or
                                 r.get("name", "?"))
                st.markdown(f"**Cart breakdown for {product_label}:**")
                line_rows = []
                for ln in lines:
                    discounted = ln.get("is_discounted")
                    line_rows.append({
                        "": "🏷" if discounted else "—",
                        "line item": (ln.get("name") or ln.get("url_slug") or "?")[:60],
                        "qty": ln.get("quantity"),
                        "original / unit": f"{ln.get('original_unit_price_czk', 0):g} CZK",
                        "after / unit":    f"{ln.get('current_unit_price_czk', 0):g} CZK",
                        "% off":           f"{ln.get('discount_pct', 0):g}%" if discounted else "—",
                    })
                st.dataframe(
                    pd.DataFrame(line_rows),
                    use_container_width=True,
                    hide_index=True,
                )

            # Negative-test info (only show interesting outcomes).
            neg_messages = sorted({
                _negative_plain(r.get("negative_status")) for r in rows
                if _negative_plain(r.get("negative_status"))
            })
            for msg in neg_messages:
                st.markdown(msg)

            # Flash-check, if any product reports a problem.
            flash_problems = [
                r for r in rows
                if r.get("flash_check") and r.get("flash_check") not in ("ok", "skipped")
            ]
            if flash_problems:
                examples = {r.get("flash_check") for r in flash_problems}
                st.markdown(
                    "⚠ **Expected text on the cart not found** — "
                    f"check `expected_flash_contains`: {', '.join(examples)}"
                )

            # Per-row failure detail (only if there are problems).
            for r in rows:
                if r["status"] in ("FAIL", "NOT_APPLIED", "ERROR"):
                    st.markdown(
                        f"- **{_product_slug(r.get('product_url', '')) or '?'}**: "
                        f"{r.get('detail', '(no detail)')}"
                    )
                    if r.get("diagnostics", {}).get("screenshot"):
                        shot = Path(r["diagnostics"]["screenshot"])
                        if shot.exists():
                            st.image(str(shot), use_container_width=True,
                                     caption=f"Page seen when {r['code']} failed on "
                                             f"{_product_slug(r.get('product_url', ''))}")

    # Advanced details — collapsed by default.
    with st.expander("🔬 Advanced — raw table"):
        df = results_dataframe(report)
        st.dataframe(df, use_container_width=True, hide_index=True)

    with st.expander("📄 Raw JSON"):
        st.json(report)


def run_tests_streaming(placeholder, headed: bool,
                        only_codes: list[str] | None = None) -> int:
    """Run run_tests.py as a subprocess and stream its output into the UI."""
    cmd = [sys.executable, "run_tests.py"]
    if headed:
        cmd.append("--headed")
    if only_codes:
        cmd.append("--only")
        cmd.extend(only_codes)
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    log_lines: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        log_lines.append(line.rstrip())
        # Show only the most recent 30 lines, kept tight.
        placeholder.code("\n".join(log_lines[-30:]) or "starting...", language="text")
    proc.wait()
    return proc.returncode


# ---------- UI ----------

st.set_page_config(
    page_title="Alensa Discount Tester",
    page_icon="🛒",
    layout="wide",
)

st.title("🛒 Alensa Discount Tester")
st.caption(
    "Browse configured discounts, run the test suite, and review reports. "
    "Tests run via Playwright against alensa.cz and stop **before payment** — "
    "see [README](https://github.com/bgal-png/Discount-tester) for the safety model."
)

# --- Sidebar: config status ---
with st.sidebar:
    st.header("Configuration")
    try:
        cfg = load_config(CONFIG_PATH)
        st.success(f"✅ Loaded `{CONFIG_PATH.name}`")
        active = [d for d in cfg.discounts if d.active]
        st.metric("Site", cfg.site)
        st.metric("Discounts (active / total)", f"{len(active)} / {len(cfg.discounts)}")
    except ConfigError as e:
        st.error(f"Config error: {e}")
        cfg = None

    st.divider()
    st.caption(f"Config file:\n`{CONFIG_PATH}`")
    st.caption(f"Reports folder:\n`{REPORTS_DIR}`")


# --- Tabs ---
tab_run, tab_discounts, tab_add, tab_edit, tab_reports = st.tabs(
    ["▶ Run tests", "📋 Discounts", "➕ Add discount",
     "📝 Edit / Delete", "📂 Past reports"]
)


def _load_raw_config() -> dict:
    """Return the parsed JSON contents of discounts.json (no schema validation)."""
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _write_raw_config(doc: dict) -> None:
    """Pretty-print and write discounts.json."""
    CONFIG_PATH.write_text(
        json.dumps(doc, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _validate_discount_dict(d: dict) -> Optional[str]:
    """Return None if the dict parses cleanly, else a human-readable error."""
    from src.config import _parse_discount  # type: ignore
    try:
        _parse_discount(d, 0)
        return None
    except (ConfigError, ValueError, KeyError, TypeError) as e:
        return str(e)


def _csv_to_list(raw: str) -> list[str]:
    """Comma-separated text -> list of trimmed non-empty strings."""
    return [s.strip() for s in (raw or "").split(",") if s.strip()]


# product_type options shown to the human, derived from the loader's allowed set
from src.config import ALLOWED_PRODUCT_TYPES as _ALLOWED_PT  # noqa: E402
PRODUCT_TYPE_OPTIONS = ["(none)"] + sorted(t for t in _ALLOWED_PT if t)


# ---- Run tab ----
with tab_run:
    st.subheader("Run the test suite")

    if not cfg:
        st.warning("Fix the configuration error first (see sidebar).")
    else:
        active = [d for d in cfg.discounts if d.active]
        all_codes = [d.code for d in cfg.discounts]
        active_codes = [d.code for d in active]

        st.markdown("**Which discount(s) to run?**")

        # Pre-baked picker presets so the common cases are one click.
        if "run_selection" not in st.session_state:
            st.session_state.run_selection = list(active_codes)

        preset_cols = st.columns([1, 1, 1, 3])
        with preset_cols[0]:
            if st.button(f"All active ({len(active_codes)})",
                         use_container_width=True,
                         disabled=not active_codes):
                st.session_state.run_selection = list(active_codes)
                st.rerun()
        with preset_cols[1]:
            if st.button(f"All ({len(all_codes)})", use_container_width=True,
                         disabled=not all_codes):
                st.session_state.run_selection = list(all_codes)
                st.rerun()
        with preset_cols[2]:
            if st.button("Clear", use_container_width=True,
                         disabled=not all_codes):
                st.session_state.run_selection = []
                st.rerun()

        # The actual multi-select — labels show active/inactive status.
        def _format(code: str) -> str:
            d = next((d for d in cfg.discounts if d.code == code), None)
            if d is None:
                return code
            flag = "🟢" if d.active else "⚫"
            return f"{flag} {d.code} — {d.name}"

        selected = st.multiselect(
            "Pick discounts",
            options=all_codes,
            default=st.session_state.run_selection,
            format_func=_format,
            label_visibility="collapsed",
            key="run_selection_picker",
        )
        # Sync the picker's state back into our preset-controllable state
        # so the preset buttons can override on the next rerun.
        st.session_state.run_selection = selected

        cols = st.columns([1, 1, 2])
        with cols[0]:
            headed = st.checkbox(
                "Show browser",
                value=False,
                help="Run in a visible Chromium window instead of headless.",
            )
        with cols[1]:
            n_to_run = len(selected)
            run_clicked = st.button(
                f"▶ Run {n_to_run} discount(s)" if n_to_run else "▶ Run",
                type="primary",
                disabled=n_to_run == 0,
            )
        with cols[2]:
            st.caption(
                "Headless ≈ 8–12 s per (discount × product). `--only` is "
                "passed when you don't pick all active, so inactive entries "
                "still run if explicitly selected."
            )

        if run_clicked:
            log_placeholder = st.empty()
            with st.spinner("Running..."):
                # If selection happens to equal exactly the active set,
                # let the runner pick them via its default 'active' filter
                # — keeps the report unambiguous. Otherwise pass --only.
                only = None
                if set(selected) != set(active_codes):
                    only = selected
                rc = run_tests_streaming(log_placeholder, headed=headed,
                                         only_codes=only)
            if rc == 0:
                st.success("Run finished. Latest report below.")
            else:
                st.error(f"Runner exited with code {rc}. Check the log above.")

            # Show latest report.
            latest = list_reports()
            if latest:
                report = load_report(latest[0])
                st.subheader(f"Latest report")
                render_human_report(report, key_prefix="run")


# ---- Discounts tab ----
with tab_discounts:
    st.subheader("Configured discounts")
    if cfg:
        rows = []
        for d in cfg.discounts:
            rows.append({
                "active": "✅" if d.active else "—",
                "code": d.code,
                "name": d.name,
                "type": d.discount_type,
                "value": d.value,
                "tol pp": d.tolerance_pct,
                "expires": d.expires_at or "",
                "product_url": d.test_product_url or "",
                "expected flash phrases": ", ".join(d.expected_flash_contains),
                "notes": (d.notes or "")[:120],
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        st.caption(
            f"Edit `{CONFIG_PATH.relative_to(ROOT)}` to change. After saving, "
            "reload this page (R). Run `python -m src.config config/discounts.json` "
            "in a terminal first to catch JSON typos."
        )
    else:
        st.info("Config not loaded.")


# ---- Add discount tab ----
with tab_add:
    st.subheader("Add a new discount")
    st.caption(
        "Fill the fields below from the backend's discount edit page and "
        "hit Save. New entries are appended to `discounts.json` with "
        "`active: false` so they don't run until you verify them."
    )

    with st.form("new_discount_form", clear_on_submit=False):
        # --- Identity ---
        st.markdown("**Identity**")
        c1, c2 = st.columns(2)
        with c1:
            f_code = st.text_input(
                "Code *",
                help="The coupon code shoppers type at checkout (e.g. SUMMER10).",
            )
            f_name = st.text_input(
                "Display name *",
                help="What shows in reports. Often the backend's Jméno/Name field.",
            )
        with c2:
            f_type = st.selectbox("Discount type *", ["percentage", "fixed_amount"])
            f_value = st.number_input(
                "Value (% or CZK) *", min_value=0.0, step=1.0, value=0.0,
                help="Backend's Percent or FIX price.",
            )
            f_tol = st.number_input(
                "Tolerance (percentage points)",
                min_value=0.0, value=1.0, step=0.5,
                help="Pass/fail band around the expected value. ±1pp suits most.",
            )

        f_description = st.text_area(
            "Description (internal note)", height=70,
            help="Backend's 'Popis' field — your team's note, not shown to customers.",
        )
        f_active = st.checkbox(
            "Active",
            value=False,
            help="Off by default. Verify with a single-discount run, then flip on.",
        )

        st.markdown("---")
        # --- Applies to ---
        st.markdown("**Applies to** — what the discount targets")
        c1, c2 = st.columns(2)
        with c1:
            f_brand = st.text_input(
                "Brand(s)",
                help=("Single brand (e.g. 'Gelone'), or comma-separated for "
                      "multi-brand (e.g. 'Crullé, Polaroid, Tommy Hilfiger'). "
                      "Leave empty for 'any brand'."),
            )
            f_excluded = st.text_input(
                "Excluded brands (comma-separated)",
                help=("Brands explicitly excluded — backend's "
                      "'Vyřazené kategorie'."),
            )
        with c2:
            f_ptype = st.selectbox(
                "Product type",
                options=PRODUCT_TYPE_OPTIONS,
                help=("Maps to the category page the runner will pick test "
                      "products from. '(none)' = any."),
            )

        st.markdown("---")
        # --- Conditions ---
        st.markdown("**Conditions** (informational unless we explicitly support them)")
        c1, c2 = st.columns(2)
        with c1:
            f_min_basket = st.number_input(
                "Min basket CZK (0 = none)",
                min_value=0, value=0,
                help="Backend's 'From price'.",
            )
            f_usage_limit = st.number_input(
                "Usage limit (0 = unlimited)",
                min_value=0, value=0,
            )
        with c2:
            f_min_items = st.number_input(
                "Min items (0 = none)",
                min_value=0, value=0,
                help="Backend's 'Min items amount'.",
            )
            f_combo_forbidden = st.checkbox(
                "Combination forbidden",
                value=False,
                help="Backend's 'Zákaz kombinování slev'.",
            )

        f_expires = st.date_input(
            "Expires at (leave empty for no expiry)", value=None,
        )

        st.markdown("---")
        # --- Validation ---
        st.markdown("**Validation**")
        f_flash = st.text_input(
            "Expected flash phrases (comma-separated)",
            help=("Substrings to look for in the cart's applied-coupon chip "
                  "after the code applies. e.g. 'CODE123, -10%'."),
        )

        st.markdown("---")
        # --- Manual overrides ---
        st.markdown("**Manual overrides** — leave empty to let the runner auto-discover")
        c1, c2 = st.columns(2)
        with c1:
            f_test_url = st.text_input(
                "Test product URL",
                help="Force-test against this exact product instead of auto-discovery.",
            )
        with c2:
            f_neg_url = st.text_input(
                "Non-matching product URL",
                help="Force-pick this product for the negative test.",
            )

        f_notes = st.text_area("Notes (free text)", height=80)

        st.markdown("")
        submitted = st.form_submit_button("💾 Save new discount", type="primary")

    if submitted:
        errors: list[str] = []
        if not f_code.strip():
            errors.append("Code is required.")
        if not f_name.strip():
            errors.append("Display name is required.")
        if f_value <= 0:
            errors.append("Value must be > 0.")

        # Check uniqueness against existing
        try:
            existing_doc = _load_raw_config()
            existing_codes = {
                str(d.get("code", "")).lower()
                for d in existing_doc.get("discounts", [])
            }
            if f_code.strip().lower() in existing_codes:
                errors.append(
                    f"Code '{f_code.strip()}' already exists — use **📝 Edit "
                    "/ Delete** to modify it."
                )
        except Exception as e:
            errors.append(f"Couldn't read existing config: {e}")
            existing_doc = None

        if errors:
            for e in errors:
                st.error(e)
        else:
            brand_value: object
            brand_list = _csv_to_list(f_brand)
            if len(brand_list) > 1:
                brand_value = brand_list
            elif len(brand_list) == 1:
                brand_value = brand_list[0]
            else:
                brand_value = None

            new_discount = {
                "name": f_name.strip(),
                "code": f_code.strip(),
                "description": f_description.strip(),
                "active": bool(f_active),
                "discount_type": f_type,
                "value": float(f_value),
                "tolerance_pct": float(f_tol),
                "test_product_url": f_test_url.strip() or None,
                "non_matching_product_url": f_neg_url.strip() or None,
                "expected_flash_contains": _csv_to_list(f_flash),
                "expires_at": (f_expires.isoformat()
                               if isinstance(f_expires, date) else None),
                "usage_limit": int(f_usage_limit) if f_usage_limit > 0 else None,
                "combination_forbidden": bool(f_combo_forbidden),
                "min_items": int(f_min_items) if f_min_items > 0 else None,
                "applies_to": {
                    "brand": brand_value,
                    "product_type": (None if f_ptype == "(none)" else f_ptype),
                    "excluded_brands": _csv_to_list(f_excluded),
                },
                "conditions": {
                    "min_basket_czk": (float(f_min_basket)
                                       if f_min_basket > 0 else None),
                    "delivery_method": None,
                },
                "notes": f_notes.strip(),
            }

            schema_err = _validate_discount_dict(new_discount)
            if schema_err:
                st.error(f"Schema validation failed: {schema_err}")
            else:
                try:
                    existing_doc.setdefault("discounts", []).append(new_discount)
                    _write_raw_config(existing_doc)
                    st.success(
                        f"✅ Saved '{new_discount['code']}' to "
                        f"{CONFIG_PATH.name}. Switch to **▶ Run tests** to "
                        "verify with `--only` before activating."
                    )
                    st.balloons()
                except Exception as e:
                    st.error(f"Failed to write file: {e}")


# ---- Edit / Delete tab ----
with tab_edit:
    st.subheader("Edit or delete a discount")
    st.caption(
        "Pick a discount to edit its JSON in isolation. Save writes back "
        "into `discounts.json` in place. Delete removes only that entry — "
        "the rest of the file is untouched."
    )

    try:
        doc = _load_raw_config()
    except Exception as e:
        st.error(f"Couldn't load {CONFIG_PATH}: {e}")
        doc = None

    if not doc or not doc.get("discounts"):
        st.info("No discounts to edit yet. Add one in the **➕ Add discount** tab.")
    else:
        codes = [str(d.get("code", "")) for d in doc["discounts"]]
        labels = [
            f"{'🟢' if d.get('active') else '⚫'} {d.get('code', '?')} — {d.get('name', '?')}"
            for d in doc["discounts"]
        ]

        # Key changes when the doc changes so the picker resets sensibly.
        picker_idx = st.selectbox(
            "Pick a discount",
            range(len(codes)),
            format_func=lambda i: labels[i],
            key=f"edit_picker_{len(codes)}",
        )
        current_entry = doc["discounts"][picker_idx]
        current_code = codes[picker_idx]
        current_text = json.dumps(current_entry, indent=2, ensure_ascii=False)

        # Per-entry session state — re-initialise when picker changes.
        edit_key = f"edit_text_{current_code}"
        if edit_key not in st.session_state:
            st.session_state[edit_key] = current_text

        # Live-validate
        edit_err = None
        edit_parsed = None
        try:
            edit_parsed = json.loads(st.session_state[edit_key])
            err = _validate_discount_dict(edit_parsed)
            if err:
                edit_err = f"Schema error: {err}"
        except json.JSONDecodeError as e:
            edit_err = f"JSON syntax error: {e}"

        edit_changed = (
            st.session_state[edit_key].strip() != current_text.strip()
        )

        c1, c2, c3, _ = st.columns([1.2, 1.2, 1, 3])
        with c1:
            save_clicked = st.button(
                "💾 Save changes",
                type="primary",
                disabled=(edit_err is not None or not edit_changed),
                key="edit_save_btn",
            )
        with c2:
            if st.button("↺ Reset to file", key="edit_reset_btn"):
                st.session_state[edit_key] = current_text
                st.rerun()
        with c3:
            confirm_delete = st.checkbox(
                "Confirm",
                key=f"confirm_del_{current_code}",
                help="Required to enable the Delete button.",
            )
        delete_clicked = st.button(
            f"🗑 Delete '{current_code}'",
            disabled=not confirm_delete,
            key="edit_delete_btn",
        )

        if save_clicked and edit_parsed is not None:
            try:
                # If the code changed, prevent collision with another entry
                new_code = str(edit_parsed.get("code", ""))
                other_codes = {c.lower() for i, c in enumerate(codes)
                               if i != picker_idx}
                if new_code.lower() in other_codes:
                    st.error(
                        f"Can't save: code '{new_code}' is already used by "
                        "another discount."
                    )
                else:
                    doc["discounts"][picker_idx] = edit_parsed
                    _write_raw_config(doc)
                    # Drop the per-entry state so the next render picks up
                    # the freshly-saved version.
                    st.session_state.pop(edit_key, None)
                    st.success(f"✅ Saved changes to '{new_code}'.")
                    st.rerun()
            except Exception as e:
                st.error(f"Failed to write file: {e}")

        if delete_clicked:
            try:
                del doc["discounts"][picker_idx]
                _write_raw_config(doc)
                st.session_state.pop(edit_key, None)
                st.session_state.pop(f"confirm_del_{current_code}", None)
                st.success(f"🗑 Deleted '{current_code}'.")
                st.rerun()
            except Exception as e:
                st.error(f"Failed to delete: {e}")

        st.text_area(
            "JSON for this discount",
            height=400,
            key=edit_key,
            label_visibility="collapsed",
        )

        if edit_err:
            st.error(edit_err)
        elif edit_changed:
            st.info("✅ Valid. **Edits NOT yet saved** — click 💾 Save changes.")
        else:
            st.success("✅ Valid. Matches the file on disk.")

        # Escape-hatch: full-file editor as before, hidden by default.
        with st.expander("Advanced — edit the full discounts.json"):
            st.caption(
                "For bulk edits or recovering from a broken state. Saves "
                "overwrite the whole file."
            )
            _RAW_FULL = "raw_full_text"
            disk_text = CONFIG_PATH.read_text(encoding="utf-8")
            if _RAW_FULL not in st.session_state:
                st.session_state[_RAW_FULL] = disk_text

            raw_err = None
            try:
                raw_parsed = json.loads(st.session_state[_RAW_FULL])
                from src.config import _parse_discount  # type: ignore
                for i, x in enumerate(raw_parsed.get("discounts", [])):
                    _parse_discount(x, i)
            except json.JSONDecodeError as e:
                raw_err = f"JSON syntax error: {e}"
            except (ConfigError, ValueError, KeyError, TypeError) as e:
                raw_err = f"Schema error: {e}"

            raw_changed = st.session_state[_RAW_FULL].strip() != disk_text.strip()

            raw_save = st.button(
                "💾 Save full file",
                type="primary",
                disabled=raw_err is not None or not raw_changed,
                key="raw_save_btn",
            )
            if raw_save:
                CONFIG_PATH.write_text(
                    st.session_state[_RAW_FULL], encoding="utf-8"
                )
                st.session_state.pop(_RAW_FULL, None)
                st.success("✅ Saved full file.")
                st.rerun()

            st.text_area(
                "Full JSON",
                height=400,
                key=_RAW_FULL,
                label_visibility="collapsed",
            )
            if raw_err:
                st.error(raw_err)


# ---- Reports tab ----
with tab_reports:
    st.subheader("Past runs")
    reports = list_reports()
    if not reports:
        st.info("No reports yet — run the suite from the **Run tests** tab.")
    else:
        labels = [
            f"{_humanize_run_timestamp(p.stem.replace('run_', ''))}  ({p.name})"
            for p in reports
        ]
        idx = st.selectbox("Pick a run", range(len(reports)),
                           format_func=lambda i: labels[i])
        path = reports[idx]
        report = load_report(path)
        render_human_report(report, key_prefix=f"hist_{path.stem}")

        st.download_button(
            "⬇ Download this report (JSON)",
            data=json.dumps(report, indent=2, ensure_ascii=False),
            file_name=path.name,
            mime="application/json",
        )
