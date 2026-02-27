import configparser
import csv
import json
import re
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Dict, List, Optional, Tuple

from NorenRestApiPy.NorenApi import BuyorSell, PriceType, ProductType

from market_cache import INSTRUMENTS, get_spot_ltp, nearest_atm_strike, strike_window_around_atm
from master_contracts import download_master_contracts
from trade_actions import (
    FlattradeApi,
    _enum_or_wrapped,
    _method_expects_enum,
    load_credentials,
    login_from_creds,
    save_credentials,
    setup_logger,
)


ORDER_EXCHANGE = {
    "NIFTY": "NFO",
    "BANKNIFTY": "NFO",
    "FINNIFTY": "NFO",
    "MIDCPNIFTY": "NFO",
    "SENSEX": "BFO",
}

LOTS_MIN = 1
LOTS_MAX = 100000000
LIMIT_PRICE_MIN = 0.0
LIMIT_PRICE_MAX = 100000000.0
# Customizable default step for Limit Price spin buttons / Up-Down keys.
LIMIT_PRICE_STEP = 0.05


class OrderRow:
    def __init__(
        self,
        parent: ttk.Frame,
        title: str,
        primary_action: str,
        freeze_lots: Dict[str, int],
        on_primary,
        on_cancel,
        on_instrument_change,
        on_expiry_change,
        on_option_change,
        on_strike_key,
        on_lots_change,
        row_index: int,
    ) -> None:
        self.row_index = row_index
        self.primary_action = primary_action
        self.freeze_lots = freeze_lots
        self.on_primary = on_primary
        self.on_cancel = on_cancel
        self.on_instrument_change = on_instrument_change
        self.on_expiry_change = on_expiry_change
        self.on_option_change = on_option_change
        self.on_strike_key = on_strike_key
        self.on_lots_change = on_lots_change

        self.current_strike_values: List[int] = []
        self.last_order_ids: List[str] = []
        self.active_bg = "#ffffff"
        self.default_bg = "#f0f0f0"
        self.header_bg = "#dcdad5"
        self._text_labels: List[tk.Label] = []

        # Row header sits above the highlighted body block.
        row_wrap = tk.Frame(parent, bd=0, highlightthickness=0)
        row_wrap.grid(row=row_index, column=0, sticky="w", padx=8, pady=6)
        row_wrap.columnconfigure(0, weight=1)
        self.row_wrap = row_wrap

        self.header_bar = tk.Frame(row_wrap, bd=0, highlightthickness=0, bg=self.header_bg)
        self.header_bar.grid(row=0, column=0, sticky="ew", pady=(0, 2))
        self.header_label = tk.Label(
            self.header_bar,
            text=title,
            bd=0,
            relief="flat",
            highlightthickness=0,
            anchor="w",
            bg=self.header_bg,
        )
        self.header_label.grid(row=0, column=0, sticky="w", padx=(4, 0), pady=(1, 1))

        container = tk.Frame(row_wrap, bd=1, relief="groove", padx=8, pady=8)
        container.grid(row=1, column=0, sticky="ew")
        self.container = container

        self.instrument_var = tk.StringVar(value="NIFTY")
        self.exchange_var = tk.StringVar(value="NFO")
        self.ordertype_var = tk.StringVar(value="LMT")
        self.expiry_var = tk.StringVar()
        self.strike_var = tk.StringVar()
        self.option_var = tk.StringVar(value="CE")
        self.lots_var = tk.StringVar(value="1")
        self.limit_price_var = tk.StringVar(value="")
        self.qty_hint_var = tk.StringVar(value="Qty (Lots: 0)")
        self.freeze_hint_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Ready")

        self._add_label(0, "Exchange")
        self.exchange_text = tk.Label(container, textvariable=self.exchange_var, bd=0, relief="flat", highlightthickness=0)
        self.exchange_text.grid(row=1, column=0, padx=3, pady=2, sticky="w")
        self._text_labels.append(self.exchange_text)

        self._add_label(1, "Instrument")
        self.instrument = ttk.Combobox(container, width=12, state="readonly", textvariable=self.instrument_var)
        self.instrument["values"] = INSTRUMENTS
        self.instrument.grid(row=1, column=1, padx=3, pady=2, sticky="ew")
        self.instrument.bind("<<ComboboxSelected>>", lambda _e: self.on_instrument_change(self))
        self.instrument.bind("<Up>", self._open_dropdown_from_up)

        self._add_label(2, "OrderType")
        self.ordertype = ttk.Combobox(container, width=8, state="readonly", textvariable=self.ordertype_var)
        self.ordertype["values"] = ("MKT", "LMT")
        self.ordertype.grid(row=1, column=2, padx=3, pady=2, sticky="ew")
        self.ordertype.bind("<<ComboboxSelected>>", lambda _e: self.toggle_limit_price())
        self.ordertype.bind("<Up>", self._open_dropdown_from_up)

        self._add_label(3, "Expiry")
        self.expiry = ttk.Combobox(container, width=12, state="readonly", textvariable=self.expiry_var)
        self.expiry.grid(row=1, column=3, padx=3, pady=2, sticky="ew")
        self.expiry.bind("<<ComboboxSelected>>", lambda _e: self.on_expiry_change(self))
        self.expiry.bind("<Up>", self._open_dropdown_from_up)

        self._add_label(4, "Strike")
        self.strike = ttk.Combobox(container, width=10, state="normal", textvariable=self.strike_var)
        self.strike.grid(row=1, column=4, padx=3, pady=2, sticky="ew")
        self.strike.bind("<KeyRelease>", lambda e: self.on_strike_key(self, e))
        self.strike.configure(postcommand=lambda: self.on_strike_key(self, None))
        self.strike.bind("<Up>", self._open_dropdown_from_up)

        self._add_label(5, "CE/PE")
        self.option = ttk.Combobox(container, width=6, state="readonly", textvariable=self.option_var)
        self.option["values"] = ("CE", "PE")
        self.option.grid(row=1, column=5, padx=3, pady=2, sticky="ew")
        self.option.bind("<<ComboboxSelected>>", lambda _e: self.on_option_change(self))
        self.option.bind("<Up>", self._open_dropdown_from_up)

        self.qty_label = tk.Label(container, textvariable=self.qty_hint_var, bd=0, relief="flat", highlightthickness=0)
        self.qty_label.grid(row=0, column=6, padx=3, pady=1, sticky="w")
        self.qty_label.configure(width=14, anchor="w")
        self._text_labels.append(self.qty_label)
        self.lots = tk.Spinbox(
            container,
            from_=LOTS_MIN,
            to=LOTS_MAX,
            increment=1,
            width=8,
            textvariable=self.lots_var,
            relief="solid",
            bd=1,
            state="normal",
            command=lambda: self.on_lots_change(self),
        )
        self.lots.grid(row=1, column=6, padx=3, pady=2, sticky="w")
        self.lots.configure(bg="white")
        self.lots.bind("<KeyRelease>", lambda _e: self.on_lots_change(self))
        self.lots.bind("<Up>", lambda _e: self._spin_up(self.lots))
        self.lots.bind("<Down>", lambda _e: self._spin_down(self.lots))

        self._add_label(7, "Limit Price")
        self.limit_price = tk.Spinbox(
            container,
            from_=LIMIT_PRICE_MIN,
            to=LIMIT_PRICE_MAX,
            increment=LIMIT_PRICE_STEP,
            width=10,
            textvariable=self.limit_price_var,
            relief="solid",
            bd=1,
            state="normal",
            bg="white",
        )
        self.limit_price.grid(row=1, column=7, padx=3, pady=2, sticky="w")
        self.limit_price.bind("<Up>", lambda _e: self._spin_up(self.limit_price))
        self.limit_price.bind("<Down>", lambda _e: self._spin_down(self.limit_price))

        self.primary_button = ttk.Button(
            container,
            text=primary_action,
            command=lambda: self.on_primary(self),
            style="Panel.TButton",
        )
        self.primary_button.grid(row=1, column=8, padx=3, pady=2, sticky="ew")
        self.cancel_button = ttk.Button(
            container,
            text="CANCEL",
            command=lambda: self.on_cancel(self),
            style="Panel.TButton",
        )
        self.cancel_button.grid(row=1, column=9, padx=3, pady=2, sticky="ew")

        self.freeze_label = tk.Label(container, textvariable=self.freeze_hint_var, bd=0, relief="flat", highlightthickness=0)
        self.freeze_label.grid(row=2, column=0, columnspan=6, sticky="w", padx=3)
        self.status_label = tk.Label(container, textvariable=self.status_var, bd=0, relief="flat", highlightthickness=0)
        self.status_label.grid(row=2, column=6, columnspan=4, sticky="e", padx=3)
        self.status_label.configure(width=30, anchor="e")
        self._text_labels.extend([self.freeze_label, self.status_label])
        self._all_widgets = [
            self.instrument,
            self.ordertype,
            self.expiry,
            self.strike,
            self.option,
            self.lots,
            self.limit_price,
        ]
        self.set_active(False)
        self.toggle_limit_price()
        self.update_freeze_hint()

    def _add_label(self, col: int, text: str) -> None:
        lbl = tk.Label(self.container, text=text, bd=0, relief="flat", highlightthickness=0)
        lbl.grid(row=0, column=col, padx=3, pady=1, sticky="w")
        self._text_labels.append(lbl)

    def _open_dropdown_from_up(self, event):
        event.widget.event_generate("<Down>")
        return "break"

    def _spin_up(self, spinbox: tk.Spinbox):
        if spinbox == self.lots:
            lot_size = max(int(float(spinbox.cget("increment"))), 1)
            raw = re.sub(r"[^0-9]", "", self.lots_var.get().strip())
            qty = int(raw) if raw else lot_size
            nearest_lots = max(1, int((qty / lot_size) + 0.5))
            next_qty = (nearest_lots + 1) * lot_size
            self.lots_var.set(str(next_qty))
            self.on_lots_change(self)
            return "break"
        spinbox.invoke("buttonup")
        return "break"

    def _spin_down(self, spinbox: tk.Spinbox):
        if spinbox == self.lots:
            lot_size = max(int(float(spinbox.cget("increment"))), 1)
            raw = re.sub(r"[^0-9]", "", self.lots_var.get().strip())
            qty = int(raw) if raw else lot_size
            nearest_lots = max(1, int((qty / lot_size) + 0.5))
            if qty % lot_size == 0:
                target_lots = max(1, nearest_lots - 1)
            else:
                target_lots = nearest_lots
            self.lots_var.set(str(target_lots * lot_size))
            self.on_lots_change(self)
            return "break"
        spinbox.invoke("buttondown")
        return "break"

    def set_active(self, active: bool) -> None:
        bg = self.active_bg if active else self.default_bg
        self.container.configure(bg=bg)
        self.row_wrap.configure(bg=self.default_bg)
        self.header_bar.configure(bg=self.header_bg)
        self.header_label.configure(bg=self.header_bg, fg="black")
        for widget in self._all_widgets:
            if widget in {self.lots, self.limit_price}:
                continue
            try:
                widget.configure(background=bg)
            except Exception:
                pass
            try:
                widget.configure(fieldbackground=bg)
            except Exception:
                pass
        for lbl in self._text_labels:
            lbl.configure(bg=bg, fg="black")
        self.lots.configure(bg="white")

    def update_freeze_hint(self) -> None:
        instrument = self.instrument_var.get()
        freeze = self.freeze_lots.get(instrument, 0)
        self.freeze_hint_var.set(f"Freeze split: Qty 0 (Lots: {freeze})")

    def toggle_limit_price(self) -> None:
        is_limit = self.ordertype_var.get() == "LMT"
        if is_limit:
            self.limit_price.configure(state="normal", bg="white", fg="black")
        else:
            self.limit_price_var.set("")
            self.limit_price.configure(state="disabled", disabledbackground="#e6e6e6", disabledforeground="#7a7a7a")


class FlattradeOrderApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("ORDER PANEL")
        self.root.resizable(False, False)
        self._setup_button_styles()
        self.base_dir = Path(__file__).resolve().parent
        self.logger = setup_logger(self.base_dir)
        self.freeze_lots = self._load_freeze_lots(self.base_dir / "config.ini")
        self.api = self._login()
        self.expects_enum = _method_expects_enum(self.api.place_order)
        self.cache = self._load_market_cache_from_master()

        wrapper = ttk.Frame(root, padding=8)
        wrapper.pack(fill="none", expand=False)
        wrapper.columnconfigure(0, weight=0)

        self.row1 = OrderRow(
            parent=wrapper,
            title="Order 1 (BUY)",
            primary_action="BUY",
            freeze_lots=self.freeze_lots,
            on_primary=self._handle_buy,
            on_cancel=self._handle_cancel,
            on_instrument_change=self._instrument_changed,
            on_expiry_change=self._expiry_changed,
            on_option_change=self._option_changed,
            on_strike_key=self._strike_key_changed,
            on_lots_change=self._lots_changed,
            row_index=0,
        )
        self.row1.active_bg = "#6688FF"
        self.row1.default_bg = "#f0f0f0"
        self.row_separator = tk.Frame(wrapper, height=3, bg="#8f8f8f")
        self.row_separator.grid(row=1, column=0, sticky="w", padx=8, pady=(4, 4))
        self.row2 = OrderRow(
            parent=wrapper,
            title="Order 2 (SELL)",
            primary_action="SELL",
            freeze_lots=self.freeze_lots,
            on_primary=self._handle_sell,
            on_cancel=self._handle_cancel,
            on_instrument_change=self._instrument_changed,
            on_expiry_change=self._expiry_changed,
            on_option_change=self._option_changed,
            on_strike_key=self._strike_key_changed,
            on_lots_change=self._lots_changed,
            row_index=2,
        )
        self.row2.active_bg = "#FF6E6E"
        self.row2.default_bg = "#f0f0f0"

        self._initialize_row(self.row1, "NIFTY")
        self._initialize_row(self.row2, "SENSEX")
        self._bind_row_focus(self.row1)
        self._bind_row_focus(self.row2)
        self._bind_numeric_handlers(self.row1)
        self._bind_numeric_handlers(self.row2)
        self._set_active_row(self.row1)

        # Footer text intentionally removed per UI preference.

        # Fit window to content and keep fixed size.
        self.root.update_idletasks()
        self.row_separator.configure(width=max(self.row1.container.winfo_reqwidth(), self.row2.container.winfo_reqwidth()))
        self.root.update_idletasks()
        width = wrapper.winfo_reqwidth() + 4
        height = wrapper.winfo_reqheight() + 4
        self.root.geometry(f"{width}x{height}")

    def _setup_button_styles(self) -> None:
        style = ttk.Style(self.root)
        style.configure("Panel.TButton", padding=(12, 3))
        style.map(
            "Panel.TButton",
            background=[("focus", "#ffd54f"), ("active", "#ffe082")],
            foreground=[("focus", "#000000"), ("active", "#000000")],
            relief=[("focus", "solid"), ("pressed", "sunken"), ("!pressed", "raised")],
        )

    def _load_freeze_lots(self, path: Path) -> Dict[str, int]:
        parser = configparser.ConfigParser()
        parser.read(path, encoding="utf-8")
        return {
            instrument: parser.getint("freeze_lots", instrument, fallback=1)
            for instrument in INSTRUMENTS
        }

    def _login(self) -> FlattradeApi:
        creds_path = self.base_dir / "creds.txt"
        creds = load_credentials(creds_path)
        # Force fresh token generation on every fresh app start.
        creds["session_token"] = ""
        creds["session_generated_at"] = ""
        save_credentials(creds_path, creds)
        self.logger.info("Cleared session_token in creds.txt for fresh login.")
        api = FlattradeApi()
        result = login_from_creds(api, creds, self.logger, creds_path=creds_path)
        self.logger.info("UI login success: %s", result)
        return api

    def _instrument_rows(self, rows, instrument: str):
        if instrument in {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"}:
            return [r for r in rows if r.get("Instrument") == "OPTIDX" and r.get("Symbol") == instrument]
        return [
            r
            for r in rows
            if r.get("Instrument") == "OPTIDX" and str(r.get("TradingSymbol", "")).startswith(("SENSEX", "SENSEX50"))
        ]

    def _load_market_cache_from_master(self) -> Dict[str, Dict[str, object]]:
        files = download_master_contracts(self.base_dir)
        with files["NFO"].txt_path.open("r", encoding="utf-8", newline="") as f:
            nfo_rows = list(csv.DictReader(f))
        with files["BFO"].txt_path.open("r", encoding="utf-8", newline="") as f:
            bfo_rows = list(csv.DictReader(f))

        data: Dict[str, Dict[str, object]] = {}
        for instrument in INSTRUMENTS:
            src_rows = nfo_rows if instrument in {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"} else bfo_rows
            rows = self._instrument_rows(src_rows, instrument)
            by_expiry: Dict[str, Dict[str, dict]] = {}
            strikes_by_expiry: Dict[str, set] = {}
            for row in rows:
                expiry = str(row.get("Expiry", "")).upper()
                opt = str(row.get("OptionType", "")).upper()
                if not expiry or opt not in {"CE", "PE"}:
                    continue
                try:
                    strike = int(float(row.get("StrikePrice", "0") or "0"))
                except ValueError:
                    continue
                label = f"{strike}{opt}"
                by_expiry.setdefault(expiry, {})
                by_expiry[expiry][label] = row
                strikes_by_expiry.setdefault(expiry, set()).add(strike)

            expiries = sorted(by_expiry.keys(), key=lambda x: datetime.strptime(x, "%d-%b-%Y"))[:12]
            data[instrument] = {
                "expiries": expiries,
                "contracts_by_expiry": by_expiry,
                "strikes_by_expiry": {k: sorted(v) for k, v in strikes_by_expiry.items()},
                "spot_ltp": None,
            }
            self.logger.info("Loaded %s expiries for %s from master", len(expiries), instrument)
        return data

    def _split_lots(self, instrument: str, total_lots: int):
        max_lots = self.freeze_lots.get(instrument, 1)
        chunks = []
        rem = total_lots
        while rem > 0:
            c = min(rem, max_lots)
            chunks.append(c)
            rem -= c
        return chunks

    def _bind_row_focus(self, row: OrderRow) -> None:
        widgets = [
            row.instrument,
            row.ordertype,
            row.expiry,
            row.strike,
            row.option,
            row.lots,
            row.limit_price,
        ]
        for widget in widgets:
            widget.bind("<FocusIn>", lambda _e, r=row: self._set_active_row(r), add="+")

    def _set_active_row(self, active_row: OrderRow) -> None:
        self.row1.set_active(active_row is self.row1)
        self.row2.set_active(active_row is self.row2)
        # Preserve Limit Price visual behavior after row highlighting:
        # white in LMT, disabled gray in MKT.
        self.row1.toggle_limit_price()
        self.row2.toggle_limit_price()

    def _initialize_row(self, row: OrderRow, instrument: str) -> None:
        row.instrument_var.set(instrument)
        self._instrument_changed(row)

    def _instrument_changed(self, row: OrderRow) -> None:
        instrument = row.instrument_var.get()
        row.exchange_var.set(ORDER_EXCHANGE[instrument])
        row.update_freeze_hint()
        row.option_var.set("CE")

        expiries = self.cache[instrument]["expiries"]
        row.expiry["values"] = expiries
        row.expiry_var.set(expiries[0] if expiries else "")
        self._refresh_strikes_for_row(row, refresh_spot=True)

    def _expiry_changed(self, row: OrderRow) -> None:
        self._refresh_strikes_for_row(row, refresh_spot=True)

    def _option_changed(self, row: OrderRow) -> None:
        self._snap_strike_to_nearest(row, commit=True)
        self._update_qty_hint(row)

    def _strike_key_changed(self, row: OrderRow, event) -> None:
        if event is not None and event.keysym in {"Up", "Down", "Left", "Right", "Return", "Escape", "Tab"}:
            return
        typed = re.sub(r"[^0-9]", "", row.strike_var.get().strip())
        all_values = [str(v) for v in row.current_strike_values]
        # Keep dropdown broad by default; only prefix-filter for short typed values.
        if 1 <= len(typed) <= 4:
            filtered = [v for v in all_values if v.startswith(typed)]
            row.strike["values"] = filtered if filtered else all_values
        else:
            row.strike["values"] = all_values
        self._update_qty_hint(row)

    def _lots_changed(self, row: OrderRow) -> None:
        self._normalize_qty_input(row, finalize=False)
        self._update_qty_hint(row)

    def _bind_numeric_handlers(self, row: OrderRow) -> None:
        row.lots.bind("<FocusOut>", lambda _e, r=row: self._on_lots_focus_out(r), add="+")
        row.limit_price.bind("<KeyRelease>", lambda _e, r=row: self._limit_price_changed(r, finalize=False), add="+")
        row.limit_price.bind("<FocusOut>", lambda _e, r=row: self._limit_price_changed(r, finalize=True), add="+")

    def _on_lots_focus_out(self, row: OrderRow) -> None:
        self._normalize_qty_input(row, finalize=True)
        self._update_qty_hint(row)

    def _lot_size_for_row(self, row: OrderRow) -> int:
        contract = self._selected_contract(row, autocorrect=False)
        if contract:
            return int(float(contract.get("LotSize", "0") or "0"))
        instrument = row.instrument_var.get()
        expiry = row.expiry_var.get()
        contracts = self.cache[instrument]["contracts_by_expiry"].get(expiry, {})
        if contracts:
            first_contract = next(iter(contracts.values()))
            return int(float(first_contract.get("LotSize", "0") or "0"))
        return 0

    def _sync_qty_spinbox_step(self, row: OrderRow, lot_size: int) -> None:
        step = max(lot_size, 1)
        row.lots.configure(from_=step, increment=step)

    def _nearest_lots_from_qty(self, qty: int, lot_size: int) -> int:
        if qty <= 0:
            return 0
        if lot_size <= 0:
            return qty
        return max(1, int((qty / lot_size) + 0.5))

    def _normalize_qty_input(self, row: OrderRow, finalize: bool) -> Optional[int]:
        lot_size = self._lot_size_for_row(row)
        self._sync_qty_spinbox_step(row, lot_size)
        raw = row.lots_var.get().strip()
        if not raw:
            if finalize:
                default_qty = max(lot_size, LOTS_MIN)
                row.lots_var.set(str(default_qty))
                return self._nearest_lots_from_qty(default_qty, lot_size)
            return None

        digits = re.sub(r"[^0-9]", "", raw)
        if not digits:
            if finalize:
                default_qty = max(lot_size, LOTS_MIN)
                row.lots_var.set(str(default_qty))
                return self._nearest_lots_from_qty(default_qty, lot_size)
            row.lots_var.set("")
            return None

        qty = int(digits)
        qty = max(LOTS_MIN, min(qty, LOTS_MAX))
        lots = self._nearest_lots_from_qty(qty, lot_size)
        normalized_qty = lots * lot_size if lot_size > 0 else qty
        if finalize:
            row.lots_var.set(str(normalized_qty))
        elif digits != raw:
            row.lots_var.set(digits)
        return lots

    def _limit_price_changed(self, row: OrderRow, finalize: bool) -> Optional[float]:
        raw = row.limit_price_var.get().strip()
        if not raw:
            return None

        # Keep only digits and one decimal point.
        cleaned_chars = []
        seen_dot = False
        for ch in raw:
            if ch.isdigit():
                cleaned_chars.append(ch)
            elif ch == "." and not seen_dot:
                cleaned_chars.append(ch)
                seen_dot = True
        cleaned = "".join(cleaned_chars)

        if cleaned != raw:
            row.limit_price_var.set(cleaned)
        if cleaned in {"", "."}:
            if finalize:
                row.limit_price_var.set("")
            return None

        try:
            value = float(cleaned)
        except ValueError:
            if finalize:
                row.limit_price_var.set("")
            return None

        if finalize or value < LIMIT_PRICE_MIN or value > LIMIT_PRICE_MAX:
            value = max(LIMIT_PRICE_MIN, min(value, LIMIT_PRICE_MAX))
            normalized = f"{value:.2f}".rstrip("0").rstrip(".")
            row.limit_price_var.set(normalized)
        return value

    def _refresh_strikes_for_row(self, row: OrderRow, refresh_spot: bool) -> None:
        instrument = row.instrument_var.get()
        expiry = row.expiry_var.get()
        cache = self.cache[instrument]
        if refresh_spot:
            spot = get_spot_ltp(self.api, instrument)
            if spot is not None:
                cache["spot_ltp"] = spot
        all_strikes = cache["strikes_by_expiry"].get(expiry, [])
        strike_window = strike_window_around_atm(all_strikes, cache["spot_ltp"], width_each_side=30)
        row.current_strike_values = strike_window
        row.strike["values"] = [str(v) for v in strike_window]
        atm = nearest_atm_strike(strike_window, cache["spot_ltp"])
        row.strike_var.set("" if atm is None else str(atm))
        self._snap_strike_to_nearest(row, commit=True)
        row.status_var.set(f"ATM near {cache['spot_ltp'] or 'N/A'} | shown {len(strike_window)} strikes")
        self._normalize_qty_input(row, finalize=True)
        self._update_qty_hint(row)

    def _extract_strike_number(self, text: str) -> Optional[int]:
        m = re.search(r"\d+", (text or "").strip())
        if not m:
            return None
        return int(m.group(0))

    def _snap_strike_to_nearest(self, row: OrderRow, commit: bool) -> Optional[int]:
        if not row.current_strike_values:
            return None
        typed_val = self._extract_strike_number(row.strike_var.get())
        if typed_val is None:
            nearest = row.current_strike_values[len(row.current_strike_values) // 2]
        else:
            nearest = min(row.current_strike_values, key=lambda x: abs(x - typed_val))
        if commit:
            row.strike_var.set(str(nearest))
        return nearest

    def _selected_contract(self, row: OrderRow, autocorrect: bool) -> Optional[dict]:
        instrument = row.instrument_var.get()
        expiry = row.expiry_var.get()
        opt = row.option_var.get().upper()
        strike = self._snap_strike_to_nearest(row, commit=autocorrect)
        if strike is None:
            return None
        key = f"{strike}{opt}"
        return self.cache[instrument]["contracts_by_expiry"].get(expiry, {}).get(key)

    def _update_qty_hint(self, row: OrderRow) -> None:
        lot_size = self._lot_size_for_row(row)
        self._sync_qty_spinbox_step(row, lot_size)
        raw = re.sub(r"[^0-9]", "", row.lots_var.get().strip())
        qty_units = int(raw) if raw else 0
        lots = self._nearest_lots_from_qty(qty_units, lot_size)
        row.qty_hint_var.set(f"Qty (Lots: {lots})")
        freeze_lots = self.freeze_lots.get(row.instrument_var.get(), 0)
        freeze_qty = freeze_lots * lot_size if lot_size > 0 else 0
        row.freeze_hint_var.set(f"Freeze split: Qty {freeze_qty} (Lots: {freeze_lots})")

    def _validate_row(self, row: OrderRow) -> Optional[str]:
        if not row.expiry_var.get():
            return "Select expiry."
        if not row.strike_var.get().strip():
            return "Select or type strike."
        lots = self._normalize_qty_input(row, finalize=True)
        if lots is None:
            return f"Qty must be an integer between {LOTS_MIN} and {LOTS_MAX}."
        if row.ordertype_var.get() == "LMT":
            limit_price = self._limit_price_changed(row, finalize=True)
            if limit_price is None:
                return "Limit Price is required for LMT."
            if not (LIMIT_PRICE_MIN <= limit_price <= LIMIT_PRICE_MAX):
                return f"Limit Price must be between {LIMIT_PRICE_MIN:g} and {LIMIT_PRICE_MAX:g}."
        return None

    def _as_log_text(self, payload) -> str:
        try:
            return json.dumps(payload, ensure_ascii=True, default=str)
        except Exception:
            return repr(payload)

    def _confirm_action(self, row: OrderRow, action: str) -> bool:
        instrument = row.instrument_var.get()
        expiry = row.expiry_var.get()
        strike = row.strike_var.get().strip()
        opt = row.option_var.get().upper()
        lots = self._normalize_qty_input(row, finalize=True) or 0
        qty_text = row.lots_var.get().strip()
        one_line = f"{instrument}, {expiry}, {strike}{opt}, {qty_text}(Lots:{lots})"
        if row.ordertype_var.get() == "LMT":
            limit_price = (row.limit_price_var.get() or "").strip()
            one_line = f"{one_line} @ {limit_price}"
        lines = [one_line]
        return self._show_confirm_dialog(lines)

    def _show_confirm_dialog(self, lines: List[str]) -> bool:
        result = {"confirmed": False}

        dialog = tk.Toplevel(self.root)
        dialog.title("Confirm Order")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        body = ttk.Frame(dialog, padding=12)
        body.grid(row=0, column=0, sticky="nsew")

        msg_font = tkfont.nametofont("TkDefaultFont").copy()
        msg_font.configure(size=11)

        for idx, line in enumerate(lines):
            tk.Label(body, text=line, font=msg_font, anchor="w", justify="left").grid(
                row=idx, column=0, sticky="w", pady=(0, 2)
            )

        button_row = ttk.Frame(body)
        button_row.grid(row=len(lines), column=0, pady=(8, 0), sticky="ew")
        button_row.columnconfigure(0, weight=1)
        button_row.columnconfigure(1, weight=1)

        def _finish(value: bool) -> None:
            result["confirmed"] = value
            dialog.destroy()

        no_btn = tk.Button(
            button_row,
            text="No",
            width=10,
            command=lambda: _finish(False),
            relief="raised",
            bd=1,
            highlightthickness=2,
            highlightbackground="#c0c0c0",
            highlightcolor="#c0c0c0",
        )
        yes_btn = tk.Button(
            button_row,
            text="Yes",
            width=10,
            command=lambda: _finish(True),
            relief="raised",
            bd=1,
            highlightthickness=2,
            highlightbackground="#ff8f00",
            highlightcolor="#ff8f00",
            default="active",
        )
        no_btn.grid(row=0, column=0, padx=(0, 6), sticky="ew")
        yes_btn.grid(row=0, column=1, padx=(6, 0), sticky="ew")

        def _focus_yes() -> None:
            yes_btn.focus_set()
            yes_btn.configure(highlightbackground="#ff8f00", highlightcolor="#ff8f00")
            no_btn.configure(highlightbackground="#c0c0c0", highlightcolor="#c0c0c0")

        def _focus_no() -> None:
            no_btn.focus_set()
            no_btn.configure(highlightbackground="#ff8f00", highlightcolor="#ff8f00")
            yes_btn.configure(highlightbackground="#c0c0c0", highlightcolor="#c0c0c0")

        yes_btn.bind("<FocusIn>", lambda _e: _focus_yes())
        no_btn.bind("<FocusIn>", lambda _e: _focus_no())
        yes_btn.bind("<Left>", lambda _e: (_focus_no(), "break")[1])
        no_btn.bind("<Right>", lambda _e: (_focus_yes(), "break")[1])
        yes_btn.bind("<Tab>", lambda _e: (_focus_no(), "break")[1])
        no_btn.bind("<Tab>", lambda _e: (_focus_yes(), "break")[1])
        yes_btn.bind("<ISO_Left_Tab>", lambda _e: (_focus_no(), "break")[1])
        no_btn.bind("<ISO_Left_Tab>", lambda _e: (_focus_yes(), "break")[1])
        yes_btn.bind("<Return>", lambda _e: _finish(True))
        no_btn.bind("<Return>", lambda _e: _finish(False))

        dialog.protocol("WM_DELETE_WINDOW", lambda: _finish(False))
        dialog.bind("<Escape>", lambda _e: _finish(False))
        _focus_yes()

        dialog.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - dialog.winfo_reqwidth()) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - dialog.winfo_reqheight()) // 2
        dialog.geometry(f"+{max(x, 0)}+{max(y, 0)}")

        self.root.wait_window(dialog)
        return bool(result["confirmed"])

    def _place_for_row(self, row: OrderRow, side: str) -> None:
        action = "BUY" if side == "B" else "SELL"
        self.logger.info("UI click row=%s action=%s", row.row_index + 1, action)
        if not self._confirm_action(row, action):
            row.status_var.set(f"{action} cancelled by user")
            self.logger.info("UI %s aborted by user at confirmation row=%s", action, row.row_index + 1)
            return

        error = self._validate_row(row)
        if error:
            messagebox.showerror("Validation Error", error)
            self.logger.warning("UI %s validation failed row=%s error=%s", action, row.row_index + 1, error)
            return
        contract = self._selected_contract(row, autocorrect=True)
        if not contract:
            messagebox.showerror("Contract Error", "No matching contract for selected strike/option/expiry.")
            self.logger.error("UI %s failed row=%s reason=no matching contract", action, row.row_index + 1)
            return

        instrument = row.instrument_var.get()
        ordertype = row.ordertype_var.get()
        lots = self._normalize_qty_input(row, finalize=True) or LOTS_MIN
        chunks = self._split_lots(instrument, lots)
        exchange = ORDER_EXCHANGE[instrument]
        tradingsymbol = contract["TradingSymbol"]
        lot_size = int(float(contract["LotSize"]))
        limit_price = self._limit_price_changed(row, finalize=True) if ordertype == "LMT" else 0.0
        if limit_price is None:
            limit_price = 0.0

        buy_sell = _enum_or_wrapped(side, BuyorSell, self.expects_enum)
        product = _enum_or_wrapped("M", ProductType, self.expects_enum)
        price_type = _enum_or_wrapped(ordertype, PriceType, self.expects_enum)

        order_ids: List[str] = []
        for i, chunk in enumerate(chunks, start=1):
            qty_units = chunk * lot_size
            remarks = f"ui_{row.primary_action.lower()}_{i}of{len(chunks)}"
            request_payload = {
                "buy_or_sell": buy_sell,
                "product_type": product,
                "exchange": exchange,
                "tradingsymbol": tradingsymbol,
                "quantity": qty_units,
                "discloseqty": 0,
                "price_type": price_type,
                "price": limit_price,
                "trigger_price": None,
                "retention": "DAY",
                "remarks": remarks,
            }
            self.logger.info(
                "BROKER_REQUEST place_order row=%s action=%s child=%s/%s payload=%s",
                row.row_index + 1,
                action,
                i,
                len(chunks),
                self._as_log_text(request_payload),
            )
            ret = self.api.place_order(
                buy_or_sell=buy_sell,
                product_type=product,
                exchange=exchange,
                tradingsymbol=tradingsymbol,
                quantity=qty_units,
                discloseqty=0,
                price_type=price_type,
                price=limit_price,
                trigger_price=None,
                retention="DAY",
                remarks=remarks,
            )
            self.logger.info(
                "BROKER_RESPONSE place_order row=%s action=%s child=%s/%s response=%s",
                row.row_index + 1,
                action,
                i,
                len(chunks),
                self._as_log_text(ret),
            )
            if not ret or str(ret.get("stat", "")).lower() != "ok":
                row.status_var.set(f"{row.primary_action} failed")
                self.logger.error(
                    "UI %s failed row=%s child=%s/%s broker_rejected=%s",
                    action,
                    row.row_index + 1,
                    i,
                    len(chunks),
                    self._as_log_text(ret),
                )
                messagebox.showerror("Order Failed", f"Broker rejected child order {i}: {ret}")
                return
            order_ids.append(str(ret.get("norenordno")))

        row.last_order_ids = order_ids
        row.status_var.set(f"{row.primary_action} success ({len(order_ids)} orders)")
        self.logger.info(
            "UI %s success row=%s total_children=%s order_ids=%s",
            action,
            row.row_index + 1,
            len(order_ids),
            self._as_log_text(order_ids),
        )
        messagebox.showinfo("Order Placed", f"{row.primary_action} placed.\nOrder IDs: {order_ids}")

    def _handle_buy(self, row: OrderRow) -> None:
        self._place_for_row(row, "B")

    def _handle_sell(self, row: OrderRow) -> None:
        self._place_for_row(row, "S")

    def _handle_cancel(self, row: OrderRow) -> None:
        self.logger.info("UI click row=%s action=CANCEL", row.row_index + 1)
        if not self._confirm_action(row, "CANCEL"):
            row.status_var.set("Cancel aborted by user")
            self.logger.info("UI CANCEL aborted by user at confirmation row=%s", row.row_index + 1)
            return

        contract = self._selected_contract(row, autocorrect=True)
        if not contract:
            messagebox.showerror("Cancel Error", "No matching contract selected to cancel.")
            self.logger.error("UI CANCEL failed row=%s reason=no matching contract", row.row_index + 1)
            return
        tsym = contract["TradingSymbol"]
        self.logger.info("UI CANCEL row=%s target_tradingsymbol=%s", row.row_index + 1, tsym)
        book = self.api.get_order_book()
        self.logger.info("BROKER_RESPONSE get_order_book row=%s response=%s", row.row_index + 1, self._as_log_text(book))
        if not book:
            self.logger.info("UI CANCEL row=%s no open orders in broker order book", row.row_index + 1)
            messagebox.showinfo("Cancel", "No open orders found.")
            return

        cancellable = []
        for item in book:
            if str(item.get("tsym", "")) != tsym:
                continue
            status = str(item.get("status", "")).upper()
            if status in {"OPEN", "TRIGGER_PENDING", "PENDING", "NEW"}:
                orderno = item.get("norenordno")
                if orderno:
                    cancellable.append(str(orderno))

        if not cancellable:
            self.logger.info("UI CANCEL row=%s no cancellable orders for %s", row.row_index + 1, tsym)
            messagebox.showinfo("Cancel", f"No open orders for {tsym}.")
            return

        cancelled = []
        failed = []
        for orderno in cancellable:
            self.logger.info("BROKER_REQUEST cancel_order row=%s orderno=%s", row.row_index + 1, orderno)
            ret = self.api.cancel_order(orderno=orderno)
            self.logger.info(
                "BROKER_RESPONSE cancel_order row=%s orderno=%s response=%s",
                row.row_index + 1,
                orderno,
                self._as_log_text(ret),
            )
            if ret and str(ret.get("stat", "")).lower() == "ok":
                cancelled.append(orderno)
            else:
                failed.append((orderno, ret))

        row.status_var.set(f"Cancel done: {len(cancelled)} ok, {len(failed)} failed")
        self.logger.info(
            "UI CANCEL result row=%s cancelled=%s failed=%s",
            row.row_index + 1,
            self._as_log_text(cancelled),
            self._as_log_text(failed),
        )
        if failed:
            messagebox.showwarning("Cancel Partial", f"Cancelled: {cancelled}\nFailed: {failed}")
        else:
            messagebox.showinfo("Cancel Success", f"Cancelled orders: {cancelled}")


def main() -> None:
    root = tk.Tk()
    style = ttk.Style(root)
    style.theme_use("clam")
    def _invoke_button_on_enter(event):
        event.widget.invoke()
        return "break"
    root.bind_class("TButton", "<Return>", _invoke_button_on_enter)
    root.bind_class("TButton", "<KP_Enter>", _invoke_button_on_enter)
    FlattradeOrderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
