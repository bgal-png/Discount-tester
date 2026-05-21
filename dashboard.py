"""Streamlit dashboard: configure discounts, run tests, browse reports.

Launch:
    streamlit run dashboard.py
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

from src.config import load_config, ConfigError

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config" / "discounts.json"
REPORTS_DIR = ROOT / "reports"

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


def results_dataframe(report: dict) -> pd.DataFrame:
    rows = []
    for r in report.get("results", []):
        rows.append({
            "": STATUS_COLOR.get(r["status"], ""),
            "status": r["status"],
            "code": r["code"],
            "name": r["name"],
            "expected": f"{r['expected_value']}{'%' if r['expected_type'] == 'percentage' else ' CZK'}",
            "observed %": r.get("observed_discount_pct"),
            "baseline CZK": r.get("baseline_czk"),
            "after CZK": r.get("observed_total_czk"),
            "saved CZK": r.get("observed_discount_czk"),
            "flash_check": r.get("flash_check"),
            "negative": r.get("negative_status"),
            "duration s": r.get("duration_s"),
            "detail": r.get("detail", ""),
        })
    return pd.DataFrame(rows)


def run_tests_streaming(placeholder, headed: bool) -> int:
    """Run run_tests.py as a subprocess and stream its output into the UI."""
    cmd = [sys.executable, "run_tests.py"]
    if headed:
        cmd.append("--headed")
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
tab_run, tab_discounts, tab_reports = st.tabs(["▶ Run tests", "📋 Discounts", "📂 Past reports"])


# ---- Run tab ----
with tab_run:
    st.subheader("Run the test suite")

    if not cfg:
        st.warning("Fix the configuration error first (see sidebar).")
    else:
        active = [d for d in cfg.discounts if d.active]
        cols = st.columns([1, 1, 2])
        with cols[0]:
            headed = st.checkbox("Show browser (slower)", value=False,
                                 help="Run in a visible Chromium window instead of headless.")
        with cols[1]:
            run_clicked = st.button(
                f"▶ Run {len(active)} active discount(s)",
                type="primary",
                disabled=len(active) == 0,
            )
        with cols[2]:
            st.caption("Headless ≈ 8–12 s per discount on this machine. Watch the live log below.")

        if run_clicked:
            log_placeholder = st.empty()
            with st.spinner("Running..."):
                rc = run_tests_streaming(log_placeholder, headed=headed)
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

        with st.expander("Raw JSON"):
            st.json(report)

        st.download_button(
            "Download JSON",
            data=json.dumps(report, indent=2, ensure_ascii=False),
            file_name=path.name,
            mime="application/json",
        )
