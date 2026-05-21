"""Streamlit dashboard: configure discounts, run tests, browse reports.

Launch locally:
    streamlit run dashboard.py

Or on Streamlit Community Cloud — see README "Deploy to Streamlit Cloud".
"""
from __future__ import annotations

import json
import os
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


def _detect_cloud() -> bool:
    """Detect Streamlit Community Cloud reliably.

    The official env var (STREAMLIT_RUNTIME_ENVIRONMENT) isn't actually set
    on Community Cloud despite being documented. The reliable signal is the
    mount path: Community Cloud always exposes the repo under /mount/src/.
    Belt-and-braces with a hostname check.
    """
    if str(ROOT).startswith("/mount/src/"):
        return True
    if os.environ.get("STREAMLIT_RUNTIME_ENVIRONMENT") == "cloud":
        return True
    # HOSTNAME on Streamlit Cloud containers starts with "streamlit-".
    if os.environ.get("HOSTNAME", "").startswith("streamlit-"):
        return True
    return False


IS_CLOUD = _detect_cloud()

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
    if IS_CLOUD:
        st.info(
            "**Cloud mode.** The alensa.cz WAF blocks Streamlit Cloud's "
            "datacenter IPs, so tests run on **your PC**, not here. The "
            "cloud dashboard is read-only for reports synced via GitHub. "
            "See the **Run tests** tab for how."
        )

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
    elif IS_CLOUD:
        # Cloud mode: tests can't run here because alensa.cz blocks the IP.
        # Show instructions for running locally + a file uploader for ad-hoc
        # one-off imports.
        st.warning(
            "**Tests can't run on Streamlit Cloud** — alensa.cz's WAF blocks "
            "the cloud's outbound IPs (we have a screenshot proving it). "
            "Run the tests on your own PC, then sync the report here."
        )

        st.markdown("### Run locally (your PC)")
        st.code(
            'cd "C:\\Users\\blank\\Desktop\\Random codes\\discount-tester"\n'
            "python run_tests.py --push",
            language="powershell",
        )
        st.caption(
            "`--push` commits + pushes the report to GitHub. Streamlit Cloud "
            "auto-redeploys in ~30 s, then you'll see the new run under "
            "**Past reports**. Drop `--push` if you want to commit manually."
        )

        st.markdown("### Or upload a report file directly")
        upload = st.file_uploader(
            "Drop a `run_*.json` file to view it",
            type=["json"],
            help="Ad-hoc: view a report without pushing it to GitHub. "
                 "This view is per-session.",
        )
        if upload is not None:
            try:
                report = json.loads(upload.read().decode("utf-8"))
                st.subheader(f"Uploaded: {upload.name}")
                summary = report.get("summary", {})
                st.write({k: v for k, v in summary.items() if v > 0} or summary)
                df = results_dataframe(report)
                st.dataframe(df, use_container_width=True, hide_index=True)
                render_diagnostics(report.get("results", []), key_prefix="upload")
            except Exception as e:
                st.error(f"Couldn't read that file: {e}")
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
        "Streamlit Cloud's filesystem is ephemeral, so saves here don't "
        "persist across restarts. Workflow: **edit below → validate → "
        "copy → paste into GitHub** (one click below). GitHub commit triggers "
        "an auto-redeploy."
    )

    raw_current = CONFIG_PATH.read_text(encoding="utf-8")
    if "config_text" not in st.session_state:
        st.session_state.config_text = raw_current

    btn_cols = st.columns([1, 1, 2])
    with btn_cols[0]:
        if st.button("➕ Insert blank discount"):
            try:
                doc = json.loads(st.session_state.config_text)
                doc.setdefault("discounts", []).append(BLANK_DISCOUNT_TEMPLATE)
                st.session_state.config_text = json.dumps(doc, indent=2, ensure_ascii=False)
                st.rerun()
            except json.JSONDecodeError as e:
                st.error(f"Can't insert — current JSON is invalid: {e}")
    with btn_cols[1]:
        if st.button("↺ Reload from file"):
            st.session_state.config_text = raw_current
            st.rerun()
    with btn_cols[2]:
        st.link_button("✏ Open GitHub editor →", GITHUB_EDIT_URL,
                       help="Paste your validated JSON there, commit, "
                            "Streamlit redeploys automatically.",
                       type="primary")

    edited = st.text_area(
        "JSON",
        height=500,
        key="config_text",
        label_visibility="collapsed",
    )

    # Live validation.
    err_msg = None
    discount_count = None
    try:
        parsed = json.loads(edited)
        # Schema-level validation via the same loader the runner uses.
        # Write to a temp string-loader: easiest is reusing load_config by
        # writing to a temp file in /tmp. But we can also call the internal
        # parser directly to avoid disk I/O.
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

    if err_msg:
        st.error(err_msg)
    else:
        active_count = sum(1 for d in parsed["discounts"] if d.get("active", True))
        st.success(f"✅ Valid. {discount_count} discount(s), {active_count} active.")
        diff_changed = edited.strip() != raw_current.strip()
        if diff_changed:
            st.info("Your edits differ from the file currently loaded on disk. "
                    "Commit them on GitHub to make them live.")
        st.download_button(
            "⬇ Download discounts.json",
            data=edited,
            file_name="discounts.json",
            mime="application/json",
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
