# Discount Tester

Automated tester for verifying that discounts on Alensa eshops behave as configured.
Starts with **alensa.cz** and **discount codes (coupons)**. Will be extended to other
sites and discount types.

## Daily use

After one-time setup below, day-to-day use is all in the browser:

1. **Double-click `start_dashboard.bat`** (or its desktop shortcut). A console
   window opens and your browser opens at http://localhost:8501.
2. In the **▶ Run tests** tab, click the big red button. The live log streams
   while Playwright works; results appear when done.
3. Close the console window when finished for the day.

To make a desktop shortcut: right-click `start_dashboard.bat` → **Send to** →
**Desktop (create shortcut)**. Rename to "Discount Tester" if you like.

## Safety

The tester is built to be incapable of placing a real order:

1. Navigation is locked to `alensa.cz` (URL allowlist).
2. Order-confirmation URLs abort the run immediately.
3. Every click goes through `safe_click()`, which refuses to click any button
   whose text looks like "Pay" / "Objednat" / "Závazně objednat" etc.

These guards live in [src/safety.py](src/safety.py) and have a self-test
(`python -m src.safety`).

## One-time setup

```powershell
cd "C:\Users\blank\Desktop\Random codes\discount-tester"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
```

After this, you never need PowerShell again unless you want to.

## Project layout

```
discount-tester/
├── config/
│   └── discounts.json        # discounts to test (human-editable)
├── reports/                  # JSON + TXT run reports
├── src/
│   ├── config.py             # JSON loader + validation
│   ├── safety.py             # hard guards against placing orders
│   └── sites/
│       └── alensa_cz.py      # site-specific selectors and flows
├── dashboard.py              # Streamlit UI
├── run_tests.py              # CLI test runner (the dashboard calls this)
├── start_dashboard.bat       # double-click launcher
├── explore.py / probe.py     # manual selector-discovery utilities
└── requirements.txt
```

## Discount JSON format

See [config/discounts.json](config/discounts.json). One entry per discount.
The most important fields are below; the full set is documented in the JSON
file itself and in the **📝 Edit config** tab of the dashboard.

| Field | Meaning |
|---|---|
| `name` | Human label shown in the report |
| `code` | The coupon code to apply |
| `active` | If `false`, the runner skips it |
| `discount_type` | `percentage` or `fixed_amount` |
| `value` | Number — percent (≤100) or CZK amount |
| `tolerance_pct` | ± percentage points before FAIL (default 1.0) |
| `test_product_url` | URL of a product to apply the code against |
| `expected_flash_contains` | Substrings expected in the cart's applied-coupon chip |
| `expires_at` | ISO date — runner skips codes past it |
| `notes` | Free-text for humans |

## Command-line use (optional)

The dashboard is the primary interface, but the CLI works too:

```powershell
python -m src.config config/discounts.json     # validate JSON only
python run_tests.py                            # run all active discounts
python run_tests.py --headed                   # show the browser
```

Reports land in `reports/run_<timestamp>.{json,txt}` either way.

## Roadmap

- [x] Project scaffold, config loader, safety guards
- [x] Add product to cart, read cart total
- [x] Apply discount code, read new total, compare to expected
- [x] Drive checkout up to (but not past) the payment step
- [x] Test runner that iterates all active discounts and writes a JSON report
- [x] Streamlit dashboard for running tests + browsing reports + editing config
- [ ] Contact-lens variant selection (sphere/BC/qty)
- [ ] Nightly scheduled run (Windows Task Scheduler)
- [ ] Expand to additional Alensa sites
