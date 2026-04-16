# Flatrade Project Handoff (Detailed)

This document captures the current state of the project, what was implemented, why certain choices were made, known constraints, and what to do next.

## 1) Project Goal

Build a practical index options order panel for Flattrade with:

- Two independent order rows:
  - Order 1 -> BUY + CANCEL
  - Order 2 -> SELL + CANCEL
- Instrument-based expiry/strike selection.
- ATM-based default strike.
- Lot-based quantity handling with freeze split.
- Broker order placement/cancel integration.
- Daily logs.

---

## 2) Current Main Files

Main runtime files in root:

- `app_ui.py` - Main UI and order execution workflow.
- `trade_actions.py` - Core auth helpers (`FlattradeApi`, token/session login logic, logging helpers).
- `token_login.py` - Automatic auth/token generation flow.
- `master_contracts.py` - Downloads and extracts contract masters + expiry review files.
- `market_cache.py` - Market data helpers (ATM/spot support and fallback utilities).
- `config.ini` - Per-index freeze lots.
- `creds.txt` - Credentials/session token store.
- `start_order_button.bat` - One-click launcher for `app_ui.py`.

Test-only files moved under `Testing/`:

- `Testing/order_place_test.py`
- `Testing/order_input.txt`
- `Testing/login_check.py`

---

## 3) Authentication and Session Behavior

### 3.1 Host selection

Project currently uses **PiConnectAPI** (V2-aligned) for order/quote/session calls.

- Host: `https://piconnect.flattrade.in/PiConnectAPI/`
- Websocket: `wss://piconnect.flattrade.in/PiConnectWSAPI/`

### 3.2 Startup token behavior

In `app_ui.py`, `_login()` intentionally clears token on startup:

- `session_token = ""`
- `session_generated_at = ""`

Then it runs `login_from_creds(...)` which triggers fresh token generation and session setup.  
This means each fresh UI launch gets a new token automatically.

### 3.3 Logout behavior

No explicit logout call is currently implemented on UI close / Ctrl+C.  
Session expires broker-side naturally.

---

## 4) UI Behavior (Current)

## Window and layout

- Window title: `ORDER PANEL`
- Fixed-size (non-resizable), auto-fit to content.
- Right/bottom dead space was reduced.

### 4.1 Row structure

Each row has:

- Gray header strip (`Order 1 (BUY)` / `Order 2 (SELL)`) above the body.
- Highlighted body:
  - Order 1 active row -> blue tone
  - Order 2 active row -> red tone
- Separator line between row 1 and row 2.

### 4.2 Inputs

Per row:

- Exchange (display text only, not editable)
- Instrument
- OrderType (`MKT` / `LMT`)
- Expiry
- Strike (dropdown + editable typing)
- CE/PE dropdown
- Lots input
- Limit Price (enabled for LMT, disabled/grey for MKT)

### 4.3 Strike UX

- Dropdown shows ATM-window strikes.
- Typing prefix (like `256`) filters suggestions.
- Invalid strike typing snaps to nearest valid strike before action.

### 4.4 Keyboard UX

- Up arrow opens combobox dropdown (same as Down).
- Enter key activates focused button (`BUY`, `SELL`, `CANCEL`).

### 4.5 Qty display

Dynamic label format:

- `Lots (Qty: xxxx)`

Where `Qty = lots * lot_size` from selected contract.

### 4.6 Confirmation dialogs

Before BUY/SELL/CANCEL execution, app asks confirmation with:

- Index
- Expiry
- Strike + CE/PE
- Lots

Order executes only after confirmation.

---

## 5) Contract Master and Expiry Extraction

`master_contracts.py` downloads:

- `https://api.shoonya.com/NFO_symbols.txt.zip`
- `https://api.shoonya.com/BFO_symbols.txt.zip`

Outputs:

- `data/master/NFO_symbols.txt`
- `data/master/BFO_symbols.txt`
- `data/expiries_review.json`
- `data/expiries_nifty.txt`
- `data/expiries_banknifty.txt`
- `data/expiries_sensex.txt`

### 5.1 SENSEX fix applied

SENSEX expiry extraction supports both symbol families:

- `SENSEX...` (BSXOPT/main weekly)
- `SENSEX50...` (SX50OPT)

This fixed earlier SENSEX expiry mismatch.

---

## 6) Instruments Supported

Now integrated in project:

- `NIFTY`
- `BANKNIFTY`
- `FINNIFTY`
- `MIDCPNIFTY`
- `SENSEX`

### 6.1 Exchange mapping for orders

- NIFTY/BANKNIFTY/FINNIFTY/MIDCPNIFTY -> `NFO`
- SENSEX -> `BFO`

### 6.2 Spot/ATM mapping

`market_cache.py` supports public index spot fallback:

- NIFTY -> `NIFTY 50`
- BANKNIFTY -> `NIFTY BANK`
- FINNIFTY -> `NIFTY FINANCIAL SERVICES`
- MIDCPNIFTY -> `NIFTY MIDCAP SELECT`
- SENSEX uses BSE token path fallback.

---

## 7) Freeze Split Logic

Defined in `config.ini` (lots per child order):

```ini
[freeze_lots]
NIFTY = 27
BANKNIFTY = 20
FINNIFTY = 40
MIDCPNIFTY = 70
SENSEX = 50
```

When placing order:

- lots are split into chunks by freeze limit.
- each chunk quantity sent as:
  - `qty_units = chunk_lots * lot_size`

---

## 8) Order Execution and Cancel Behavior

### 8.1 BUY/SELL

BUY in row1 and SELL in row2:

- resolves selected contract from master data
- computes lot size and split chunks
- places child orders via `self.api.place_order(...)`
- logs response per child

### 8.2 CANCEL

CANCEL for selected row currently:

- resolves currently selected symbol (`instrument + expiry + strike + CE/PE`)
- fetches order book
- cancels all matching open/pending orders for that symbol.

---

## 9) Logging

All runtime activity logs to:

- `logs/YYYYMMDD.txt`

Logs include:

- startup login/session events
- data loading
- order/cancel actions
- per-child broker response
- status summaries

---

## 10) Run Instructions

### 10.1 Main UI

Double-click:

- `start_order_button.bat`

Or run manually:

```powershell
.\.venv\Scripts\python.exe .\app_ui.py
```

### 10.2 Refresh masters only

```powershell
.\.venv\Scripts\python.exe .\master_contracts.py
```

---

## 11) Known Constraints / Notes

- No explicit broker logout on close yet.
- UI depends on successful token generation/session set each startup.
- Contract source comes from downloaded master files; if broker changes symbol format, parser may need updates.
- `Testing/` scripts are kept for debugging and standalone validation.

---

## 12) Suggested Next Enhancements

1. Add explicit logout on UI close and Ctrl+C.
2. Add a read-only “resolved tradingsymbol” display in each row for final verification.
3. Add persistent user preferences (last selected instrument/expiry).
4. Add optional order re-check (order status poll after placement).
5. Add stricter validation for limit price bands/tick-size rounding.

---

## 13) Quick Context for Future Agent

If another agent continues work, they should:

1. Start with `app_ui.py`, `trade_actions.py`, `token_login.py`.
2. Keep TP host endpoints (not API host).
3. Preserve startup fresh-token behavior unless user asks otherwise.
4. Validate any order-flow changes against `logs/YYYYMMDD.txt`.
5. Keep `Testing/` scripts for debug parity.

---

## 14) Recent Updates (March 2026)

This section records important behavior changes made after the earlier handoff.

### 14.1 Quantity/Lots UX (Row Input)

- Row input is now **Qty-based** (units), with derived lots.
- Header label format changed to:
  - `Qty (Lots: X)`
- Rounding behavior:
  - typed qty is interpreted to nearest lots by current contract lot size
  - example for NIFTY lot size 65:
    - `130` -> `Lots: 2`
    - `150` -> nearest `Lots: 2`
    - Up/Down moves by one lot step (`130 <-> 195`)
- Freeze text now shows both:
  - `Freeze split: Qty <qty> (Lots: <lots>)`

### 14.2 Confirmation Dialog UX

- Confirmation detail is now on a single line without field labels.
- Format:
  - Limit order: `NIFTY, 02-MAR-2026, 25300CE, 65(Lots:1) @ 0.2`
  - Market order: `NIFTY, 02-MAR-2026, 25300CE, 65(Lots:1)`
- Buttons changed:
  - `No` on left, `Yes` on right
  - default highlight/focus on `Yes`
  - keyboard navigation improved (Left/Right/Tab/Enter/Esc)

### 14.3 Button Focus Visibility

- `BUY/SELL/CANCEL` buttons use a stronger focus style (`Panel.TButton`) for better Tab-navigation visibility.

### 14.4 Limit Price Behavior

- `Limit Price` is a spinbox with configurable step:
  - `LIMIT_PRICE_STEP = 0.05` (default)
- `Lots/Qty` and `Limit Price` fields remain white, not row-highlight colored.

### 14.5 Order and Broker Logging

`app_ui.py` now logs full order/cancel lifecycle in `logs/YYYYMMDD.txt`:

- UI click event (`BUY`/`SELL`/`CANCEL`)
- broker request payload before order/cancel calls
- full raw broker response for:
  - `place_order`
  - `get_order_book`
  - `cancel_order`
- explicit success/failure summaries and order IDs
- user-abort and validation-failure paths

### 14.6 Login/Auth Logging

Auth/session logging was expanded in `token_login.py` and `trade_actions.py`:

- endpoint URLs (auth + trading host/ws)
- auth request payloads and responses for:
  - `/auth/session`
  - `/ftauth`
  - `/trade/apitoken`
- redirect URL, extracted request code, set_session responses
- generated/refreshed/session token values

### 14.7 Startup Token Policy (Current)

- Current startup policy is back to **fresh token every UI launch**.
- In `_login()` (`app_ui.py`):
  - clears `session_token` and `session_generated_at`
  - saves creds
  - runs `login_from_creds(...)` to generate and use a new token

### 14.8 Critical Auth Parser Fix

- `token_login._extract_request_code()` was updated to handle malformed redirect query shapes seen from broker auth, including:
  - `??code=...`
  - fallback regex extraction for non-standard query strings
- This specifically addresses failures like:
  - `Could not find request_code/request_token in RedirectURL`

### 14.9 Security Note

- Logs now intentionally include sensitive data (tokens, auth payloads, some credentials-derived values) for debugging.
- Treat `logs/*.txt` as sensitive; do not share externally without redaction.

### 14.10 V2 Resilience Fixes

- `market_cache.get_spot_ltp(...)` now uses safe wrappers around `searchscrip/get_quotes` so empty/non-JSON broker responses do not crash startup.
- If quote calls fail, app falls back to public index LTP source (existing fallback path).
- `app_ui.py` order/cancel calls are now exception-safe:
  - `place_order`, `get_order_book`, `cancel_order` exceptions are caught and logged.
  - UI now shows a controlled error popup instead of a Tkinter crash.

### 14.11 Credentials Backup File Handling

- `creds_bkp.txt` is in `.gitignore`.
- File was tracked earlier and has been removed from git index using:
  - `git rm --cached creds_bkp.txt`
- This keeps local backup file on disk but prevents future commits/pushes of it.

