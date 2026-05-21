# Discount Tester

Automated tester for verifying that discounts on Alensa eshops behave as configured.
Starts with **alensa.cz** and **discount codes (coupons)**. Will be extended to other
sites and discount types.

## Safety

The tester is built to be incapable of placing a real order:

1. Navigation is locked to `alensa.cz` (URL allowlist).
2. Order-confirmation URLs (`order-confirmation`, `objednavka-dokoncena`, etc.)
   abort the run immediately.
3. Every click goes through `safe_click()`, which refuses to click any button
   whose text looks like "Pay" / "Objednat" / "Závazně objednat" etc.

These guards live in [src/safety.py](src/safety.py) and are exercised by a
self-test (`python -m src.safety`).

## Project layout

```
discount-tester/
├── config/
│   └── discounts.json        # discounts to test (human-editable)
├── reports/                  # screenshots and run reports (gitignored)
├── src/
│   ├── config.py             # JSON loader + validation
│   ├── safety.py             # hard guards against placing orders
│   └── sites/
│       └── alensa_cz.py      # site-specific selectors and flows
├── explore.py                # manual: open alensa.cz, dump title/screenshot
├── requirements.txt
└── README.md
```

## Setup (one-time)

```powershell
cd "C:\Users\blank\Desktop\Random codes\discount-tester"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
```

## Running

Validate the config (catches typos before any browser opens):

```powershell
python -m src.config config/discounts.json
```

Open the homepage and confirm Playwright works end-to-end:

```powershell
python explore.py            # closes after a screenshot
python explore.py --pause    # stays open so you can poke around
python explore.py --headless # no visible window (for scheduled runs)
```

## Discount JSON format

See [config/discounts.json](config/discounts.json). One entry per discount:

| Field | Meaning |
|---|---|
| `name` | Human label shown in the report |
| `code` | The coupon code to type at checkout |
| `active` | If `false`, the discount is skipped |
| `discount_type` | `percentage` or `fixed_amount` |
| `value` | Number — percent (≤100) or CZK amount |
| `applies_to.brand` | Restrict to one brand (or `null`) |
| `applies_to.product_type` | `glasses`, `sunglasses`, `contact_lenses`, `solutions`, `eye_drops`, `accessories` |
| `conditions.min_basket_czk` | Minimum basket value for the code to apply |
| `conditions.delivery_method` | Restrict to a delivery method (none implemented yet) |
| `notes` | Free-text notes for humans |

## Running the full suite

```powershell
python run_tests.py            # headless, all active discounts
python run_tests.py --headed   # show the browser
```

Each active discount in `config/discounts.json` is tested in a fresh browser
context. Baselines are measured once per unique `test_product_url` and reused.
A JSON + TXT report lands in `reports/run_<timestamp>.{json,txt}`.

## Roadmap

- [x] Project scaffold, config loader, safety guards, homepage opener
- [x] Add product to cart, read cart total
- [x] Apply discount code, read new total, compare to expected
- [x] Drive checkout up to (but not past) the payment step
- [x] Test runner that iterates all active discounts and writes a JSON report
- [ ] Contact-lens variant selection (sphere/BC/qty)
- [ ] Streamlit dashboard for browsing reports
- [ ] Nightly scheduled run (Windows Task Scheduler)
- [ ] Expand to additional Alensa sites
