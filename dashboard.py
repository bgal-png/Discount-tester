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
from datetime import datetime
from pathlib import Path

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
tab_run, tab_discounts, tab_edit, tab_reports = st.tabs(
    ["▶ Run tests", "📋 Discounts", "📝 Edit config", "📂 Past reports"]
)


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
                st.subheader(f"Latest report: {latest[0].name}")
                summary = report.get("summary", {})
                st.write({k: v for k, v in summary.items() if v > 0} or summary)
                df = results_dataframe(report)
                st.dataframe(df, use_container_width=True, hide_index=True)
                render_diagnostics(report.get("results", []), key_prefix="run")


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


# ---- Edit config tab ----
with tab_edit:
    st.subheader("Edit `config/discounts.json`")
    st.caption(
        "Edit below, hit **💾 Save to disk**, then go to the **▶ Run tests** "
        "tab. Saves overwrite the file directly — use **↺ Reload** to "
        "discard local edits and pull what's on disk."
    )

    raw_current = CONFIG_PATH.read_text(encoding="utf-8")
    if "config_text" not in st.session_state:
        st.session_state.config_text = raw_current

    # We need the live validation result to decide whether Save is enabled,
    # so do the parse once up front before drawing the buttons.
    edited_for_check = st.session_state.config_text
    err_msg = None
    parsed = None
    discount_count = None
    try:
        parsed = json.loads(edited_for_check)
        from src.config import _parse_discount  # type: ignore
        if "discounts" not in parsed or not isinstance(parsed["discounts"], list):
            raise ValueError("Root must contain a 'discounts' list")
        for i, d in enumerate(parsed["discounts"]):
            _parse_discount(d, i)
        discount_count = len(parsed["discounts"])
    except json.JSONDecodeError as e:
        err_msg = f"JSON syntax error: {e}"
    except (ConfigError, ValueError, KeyError, TypeError) as e:
        err_msg = f"Schema error: {e}"

    diff_changed = edited_for_check.strip() != raw_current.strip()

    btn_cols = st.columns([1.3, 1, 1, 1.5, 2])
    with btn_cols[0]:
        save_clicked = st.button(
            "💾 Save to disk",
            type="primary",
            disabled=err_msg is not None or not diff_changed,
            help=("Write the edits below to config/discounts.json. "
                  "Disabled until the JSON is valid AND differs from disk."),
        )
    with btn_cols[1]:
        if st.button("➕ Insert blank"):
            try:
                doc = json.loads(st.session_state.config_text)
                doc.setdefault("discounts", []).append(BLANK_DISCOUNT_TEMPLATE)
                st.session_state.config_text = json.dumps(
                    doc, indent=2, ensure_ascii=False
                )
                st.rerun()
            except json.JSONDecodeError as e:
                st.error(f"Can't insert — current JSON is invalid: {e}")
    with btn_cols[2]:
        if st.button("↺ Reload"):
            st.session_state.config_text = raw_current
            st.rerun()
    with btn_cols[3]:
        st.download_button(
            "⬇ Download",
            data=st.session_state.config_text,
            file_name="discounts.json",
            mime="application/json",
            help="Save a copy somewhere safe before risky edits.",
        )
    with btn_cols[4]:
        pass  # filler

    if save_clicked:
        try:
            CONFIG_PATH.write_text(st.session_state.config_text, encoding="utf-8")
            st.success(f"✅ Saved {discount_count} discount(s) to {CONFIG_PATH.name}")
            st.rerun()  # re-read the sidebar's loaded-config view
        except OSError as e:
            st.error(f"Failed to write: {e}")

    st.text_area(
        "JSON",
        height=500,
        key="config_text",
        label_visibility="collapsed",
    )

    if err_msg:
        st.error(err_msg)
    elif parsed is not None:
        active_count = sum(1 for d in parsed["discounts"] if d.get("active", True))
        if diff_changed:
            st.info(
                f"✅ Valid. {discount_count} discount(s), {active_count} active. "
                "**Edits NOT yet saved to disk** — click 💾 Save to disk."
            )
        else:
            st.success(
                f"✅ Valid. {discount_count} discount(s), {active_count} active. "
                "Matches the file on disk."
            )


# ---- Reports tab ----
with tab_reports:
    st.subheader("Past runs")
    reports = list_reports()
    if not reports:
        st.info("No reports yet — run the suite from the **Run tests** tab.")
    else:
        labels = [f"{p.stem.replace('run_', '')}  ({p.name})" for p in reports]
        idx = st.selectbox("Pick a run", range(len(reports)),
                           format_func=lambda i: labels[i])
        path = reports[idx]
        report = load_report(path)
        meta_cols = st.columns(3)
        meta_cols[0].metric("Timestamp", report.get("timestamp", ""))
        meta_cols[1].metric("Site", report.get("site", ""))
        summary = report.get("summary", {})
        meta_cols[2].metric(
            "Pass / Fail",
            f"{summary.get('PASS', 0)} / {summary.get('FAIL', 0) + summary.get('NOT_APPLIED', 0) + summary.get('ERROR', 0)}"
        )
        st.write(summary)
        df = results_dataframe(report)
        st.dataframe(df, use_container_width=True, hide_index=True)
        render_diagnostics(report.get("results", []), key_prefix=f"hist_{path.stem}")

        with st.expander("Raw JSON"):
            st.json(report)

        st.download_button(
            "Download JSON",
            data=json.dumps(report, indent=2, ensure_ascii=False),
            file_name=path.name,
            mime="application/json",
        )
