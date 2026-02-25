import configparser
import csv
import re
import tkinter as tk
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
        self.qty_hint_var = tk.StringVar(value="Lots (Qty: 0)")
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
        self._text_labels.append(self.qty_label)
        self.lots = ttk.Entry(container, width=8, textvariable=self.lots_var)
        self.lots.grid(row=1, column=6, padx=3, pady=2, sticky="ew")
        self.lots.bind("<KeyRelease>", lambda _e: self.on_lots_change(self))

        self._add_label(7, "Limit Price")
        self.limit_price = tk.Entry(container, width=12, textvariable=self.limit_price_var, relief="solid", bd=1)
        self.limit_price.grid(row=1, column=7, padx=3, pady=2, sticky="ew")

        ttk.Button(container, text=primary_action, command=lambda: self.on_primary(self)).grid(
            row=1, column=8, padx=3, pady=2, sticky="ew"
        )
        ttk.Button(container, text="CANCEL", command=lambda: self.on_cancel(self)).grid(
            row=1, column=9, padx=3, pady=2, sticky="ew"
        )

        self.freeze_label = tk.Label(container, textvariable=self.freeze_hint_var, bd=0, relief="flat", highlightthickness=0)
        self.freeze_label.grid(row=2, column=0, columnspan=6, sticky="w", padx=3)
        self.status_label = tk.Label(container, textvariable=self.status_var, bd=0, relief="flat", highlightthickness=0)
        self.status_label.grid(row=2, column=6, columnspan=4, sticky="e", padx=3)
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

    def set_active(self, active: bool) -> None:
        bg = self.active_bg if active else self.default_bg
        self.container.configure(bg=bg)
        self.row_wrap.configure(bg=self.default_bg)
        self.header_bar.configure(bg=self.header_bg)
        self.header_label.configure(bg=self.header_bg, fg="black")
        for widget in self._all_widgets:
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

    def update_freeze_hint(self) -> None:
        instrument = self.instrument_var.get()
        freeze = self.freeze_lots.get(instrument, 0)
        self.freeze_hint_var.set(f"Freeze split: {freeze} lots per order")

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
        self._set_active_row(self.row1)

        # Footer text intentionally removed per UI preference.

        # Fit window to content and keep fixed size.
        self.root.update_idletasks()
        self.row_separator.configure(width=max(self.row1.container.winfo_reqwidth(), self.row2.container.winfo_reqwidth()))
        self.root.update_idletasks()
        width = wrapper.winfo_reqwidth() + 4
        height = wrapper.winfo_reqheight() + 4
        self.root.geometry(f"{width}x{height}")

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
        self._update_qty_hint(row)

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
        contract = self._selected_contract(row, autocorrect=False)
        lot_size = int(float(contract.get("LotSize"))) if contract else 0
        try:
            lots = int(row.lots_var.get())
            lots = max(lots, 0)
        except ValueError:
            lots = 0
        qty_units = lots * lot_size
        row.qty_hint_var.set(f"Lots (Qty: {qty_units})")

    def _validate_row(self, row: OrderRow) -> Optional[str]:
        if not row.expiry_var.get():
            return "Select expiry."
        if not row.strike_var.get().strip():
            return "Select or type strike."
        try:
            lots = int(row.lots_var.get())
            if lots <= 0:
                return "Lots must be > 0."
        except ValueError:
            return "Lots must be an integer."
        if row.ordertype_var.get() == "LMT" and not row.limit_price_var.get().strip():
            return "Limit Price is required for LMT."
        return None

    def _confirm_action(self, row: OrderRow, action: str) -> bool:
        instrument = row.instrument_var.get()
        expiry = row.expiry_var.get()
        strike = row.strike_var.get().strip()
        opt = row.option_var.get().upper()
        lots = row.lots_var.get().strip()
        msg = (
            f"Confirm {action}?\n\n"
            f"Index: {instrument}\n"
            f"Expiry: {expiry}\n"
            f"Strike: {strike}{opt}\n"
            f"Lots: {lots}"
        )
        if action == "CANCEL":
            msg += "\n\nThis will cancel all open orders for the selected strike symbol."
        return messagebox.askyesno("Confirm Order Action", msg)

    def _place_for_row(self, row: OrderRow, side: str) -> None:
        action = "BUY" if side == "B" else "SELL"
        if not self._confirm_action(row, action):
            row.status_var.set(f"{action} cancelled by user")
            return

        error = self._validate_row(row)
        if error:
            messagebox.showerror("Validation Error", error)
            return
        contract = self._selected_contract(row, autocorrect=True)
        if not contract:
            messagebox.showerror("Contract Error", "No matching contract for selected strike/option/expiry.")
            return

        instrument = row.instrument_var.get()
        ordertype = row.ordertype_var.get()
        lots = int(row.lots_var.get())
        chunks = self._split_lots(instrument, lots)
        exchange = ORDER_EXCHANGE[instrument]
        tradingsymbol = contract["TradingSymbol"]
        lot_size = int(float(contract["LotSize"]))
        limit_price = float(row.limit_price_var.get()) if ordertype == "LMT" else 0.0

        buy_sell = _enum_or_wrapped(side, BuyorSell, self.expects_enum)
        product = _enum_or_wrapped("M", ProductType, self.expects_enum)
        price_type = _enum_or_wrapped(ordertype, PriceType, self.expects_enum)

        order_ids: List[str] = []
        for i, chunk in enumerate(chunks, start=1):
            qty_units = chunk * lot_size
            remarks = f"ui_{row.primary_action.lower()}_{i}of{len(chunks)}"
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
            self.logger.info("UI child order response row=%s: %s", row.row_index + 1, ret)
            if not ret or str(ret.get("stat", "")).lower() != "ok":
                row.status_var.set(f"{row.primary_action} failed")
                messagebox.showerror("Order Failed", f"Broker rejected child order {i}: {ret}")
                return
            order_ids.append(str(ret.get("norenordno")))

        row.last_order_ids = order_ids
        row.status_var.set(f"{row.primary_action} success ({len(order_ids)} orders)")
        messagebox.showinfo("Order Placed", f"{row.primary_action} placed.\nOrder IDs: {order_ids}")

    def _handle_buy(self, row: OrderRow) -> None:
        self._place_for_row(row, "B")

    def _handle_sell(self, row: OrderRow) -> None:
        self._place_for_row(row, "S")

    def _handle_cancel(self, row: OrderRow) -> None:
        if not self._confirm_action(row, "CANCEL"):
            row.status_var.set("Cancel aborted by user")
            return

        contract = self._selected_contract(row, autocorrect=True)
        if not contract:
            messagebox.showerror("Cancel Error", "No matching contract selected to cancel.")
            return
        tsym = contract["TradingSymbol"]
        book = self.api.get_order_book()
        if not book:
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
            messagebox.showinfo("Cancel", f"No open orders for {tsym}.")
            return

        cancelled = []
        failed = []
        for orderno in cancellable:
            ret = self.api.cancel_order(orderno=orderno)
            self.logger.info("UI cancel response row=%s orderno=%s ret=%s", row.row_index + 1, orderno, ret)
            if ret and str(ret.get("stat", "")).lower() == "ok":
                cancelled.append(orderno)
            else:
                failed.append((orderno, ret))

        row.status_var.set(f"Cancel done: {len(cancelled)} ok, {len(failed)} failed")
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
