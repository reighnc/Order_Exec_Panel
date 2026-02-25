import argparse
import configparser
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import requests
from NorenRestApiPy.NorenApi import BuyorSell, PriceType, ProductType

from master_contracts import download_master_contracts
from trade_actions import (
    FlattradeApi,
    _enum_or_wrapped,
    _method_expects_enum,
    _mask_secret,
    load_credentials,
    login_from_creds,
    setup_logger,
)


def _load_freeze_lots(path: Path) -> Dict[str, int]:
    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    return {
        "NIFTY": parser.getint("freeze_lots", "NIFTY", fallback=27),
        "BANKNIFTY": parser.getint("freeze_lots", "BANKNIFTY", fallback=15),
        "SENSEX": parser.getint("freeze_lots", "SENSEX", fallback=20),
    }


def _split_lots(total_lots: int, max_per_order: int) -> List[int]:
    chunks: List[int] = []
    remaining = total_lots
    while remaining > 0:
        chunk = min(remaining, max_per_order)
        chunks.append(chunk)
        remaining -= chunk
    return chunks


def _load_master_rows(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _normalize_expiry_input(value: str) -> str:
    text = value.strip().upper()
    for fmt in ("%d-%b-%Y", "%d%b%y", "%d%b%Y"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.strftime("%d-%b-%Y").upper()
        except ValueError:
            continue
    raise ValueError("Expiry format not recognized. Use e.g. 26FEB26 or 26-FEB-2026")


def _resolve_contract(
    rows: List[Dict[str, str]],
    instrument: str,
    expiry: str,
    strike: int,
    option_type: str,
) -> Dict[str, str]:
    expiry_norm = _normalize_expiry_input(expiry)
    option_type = option_type.upper()

    def matches(row: Dict[str, str]) -> bool:
        if row.get("Instrument") != "OPTIDX":
            return False
        if row.get("OptionType") != option_type:
            return False
        if row.get("Expiry", "").upper() != expiry_norm:
            return False
        if int(float(row.get("StrikePrice", "0") or "0")) != strike:
            return False
        if instrument in {"NIFTY", "BANKNIFTY"}:
            return row.get("Symbol") == instrument
        # SENSEX in BFO master can appear as SENSEX* (BSXOPT) or SENSEX50* (SX50OPT).
        return str(row.get("TradingSymbol", "")).startswith(("SENSEX", "SENSEX50"))

    for row in rows:
        if matches(row):
            return row
    raise ValueError(
        f"Contract not found for {instrument} {expiry} {option_type} {strike}. "
        "Run master_contracts.py and check data/expiries_review.json first."
    )


def _read_order_input_file(path: Path) -> Dict[str, str]:
    data: Dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip().lower()] = value.strip()
    return data


def _pick(cli_value, file_data: Dict[str, str], key: str):
    return cli_value if cli_value not in (None, "") else file_data.get(key)


def _place_order_raw_debug(
    *,
    base_host: str,
    user_id: str,
    session_token: str,
    side: str,
    product: str,
    exchange: str,
    tradingsymbol: str,
    quantity: int,
    price_type: str,
    price: float,
    trigger_price: Optional[float],
    remarks: str,
    logger,
) -> Dict[str, object]:
    url = f"{base_host}/PlaceOrder"
    values = {
        "ordersource": "API",
        "uid": user_id,
        "actid": user_id,
        "trantype": side,
        "prd": product,
        "exch": exchange,
        "tsym": tradingsymbol,
        "qty": str(quantity),
        "dscqty": "0",
        "prctyp": price_type,
        "prc": str(price),
        "trgprc": "" if trigger_price is None else str(trigger_price),
        "ret": "DAY",
        "remarks": remarks,
    }

    payload = "jData=" + json.dumps(values) + f"&jKey={session_token}"
    logger.info("PlaceOrder URL: %s", url)
    logger.info("PlaceOrder payload (safe): %s", values)
    logger.info("Session token (masked): %s", _mask_secret(session_token))

    response = requests.post(url, data=payload, timeout=30)
    logger.info("PlaceOrder HTTP status: %s", response.status_code)
    logger.info("PlaceOrder raw response: %s", response.text)

    try:
        parsed = response.json()
    except Exception:
        parsed = {"stat": "Not_Ok", "emsg": "Non-JSON broker response", "raw": response.text}
    logger.info("PlaceOrder parsed response: %s", parsed)
    return parsed


def _validate_trading_session(
    base_host: str, user_id: str, account_id: str, session_token: str, logger
) -> Dict[str, object]:
    url = f"{base_host}/Limits"
    payload = "jData=" + json.dumps({"uid": user_id, "actid": account_id}) + f"&jKey={session_token}"
    logger.info("Session validation URL: %s", url)
    logger.info("Session validation token (masked): %s", _mask_secret(session_token))
    response = requests.post(url, data=payload, timeout=30)
    logger.info("Session validation HTTP status: %s", response.status_code)
    logger.info("Session validation raw response: %s", response.text)
    try:
        parsed = response.json()
    except Exception:
        parsed = {"stat": "Not_Ok", "emsg": "Non-JSON validation response", "raw": response.text}
    logger.info("Session validation parsed response: %s", parsed)
    return parsed


def main() -> None:
    broker_base = "https://piconnect.flattrade.in/PiConnectTP"
    parser = argparse.ArgumentParser(description="Standalone broker order placement test.")
    parser.add_argument("--input-file", default="order_input.txt", help="Key=value input file path")
    parser.add_argument("--instrument", choices=["NIFTY", "BANKNIFTY", "SENSEX"])
    parser.add_argument("--expiry", help="e.g. 26FEB26 or 26-FEB-2026")
    parser.add_argument("--strike", type=int)
    parser.add_argument("--opt", choices=["CE", "PE"])
    parser.add_argument("--side", choices=["B", "S"])
    parser.add_argument("--ordertype", choices=["MKT", "LMT"])
    parser.add_argument("--lots", type=int)
    parser.add_argument("--price", type=float, help="Required for LMT")
    parser.add_argument("--trigger", type=float, help="Required for LMT")
    parser.add_argument("--product", help="Product type (default M)")
    parser.add_argument("--remarks", default="order_place_test")
    args = parser.parse_args()

    base_dir = Path(__file__).resolve().parent
    input_path = Path(args.input_file)
    if not input_path.is_absolute():
        input_path = base_dir / input_path
    file_data = _read_order_input_file(input_path) if input_path.exists() else {}

    instrument = str(_pick(args.instrument, file_data, "instrument") or "").upper()
    expiry = str(_pick(args.expiry, file_data, "expiry") or "")
    strike_raw = _pick(args.strike, file_data, "strike")
    opt = str(_pick(args.opt, file_data, "opt") or "").upper()
    side = str(_pick(args.side, file_data, "side") or "").upper()
    ordertype = str(_pick(args.ordertype, file_data, "ordertype") or "").upper()
    lots_raw = _pick(args.lots, file_data, "lots")
    price_raw = _pick(args.price, file_data, "price")
    trigger_raw = _pick(args.trigger, file_data, "trigger")
    product = str(_pick(args.product, file_data, "product") or "M").upper()
    remarks = str(_pick(args.remarks, file_data, "remarks") or "order_place_test")

    missing = []
    for key, value in {
        "instrument": instrument,
        "expiry": expiry,
        "strike": strike_raw,
        "opt": opt,
        "side": side,
        "ordertype": ordertype,
        "lots": lots_raw,
    }.items():
        if value in (None, ""):
            missing.append(key)
    if missing:
        raise ValueError(
            f"Missing required inputs: {', '.join(missing)}. "
            f"Provide via CLI or {input_path.name}."
        )

    if instrument not in {"NIFTY", "BANKNIFTY", "SENSEX"}:
        raise ValueError("instrument must be NIFTY/BANKNIFTY/SENSEX")
    if opt not in {"CE", "PE"}:
        raise ValueError("opt must be CE or PE")
    if side not in {"B", "S"}:
        raise ValueError("side must be B or S")
    if ordertype not in {"MKT", "LMT"}:
        raise ValueError("ordertype must be MKT or LMT")

    strike = int(strike_raw)
    lots = int(lots_raw)
    price = float(price_raw) if price_raw not in (None, "") else 0.0
    trigger = float(trigger_raw) if trigger_raw not in (None, "") else 0.0

    if lots <= 0:
        raise ValueError("--lots must be positive")
    if ordertype == "LMT" and (price <= 0 or trigger <= 0):
        raise ValueError("For LMT, both --price and --trigger must be > 0")

    logger = setup_logger(base_dir)
    logger.info("----- order_place_test started -----")
    logger.info("Input file path: %s", input_path)
    logger.info(
        "Normalized inputs: instrument=%s expiry=%s strike=%s opt=%s side=%s ordertype=%s lots=%s price=%s trigger=%s product=%s remarks=%s",
        instrument,
        expiry,
        strike,
        opt,
        side,
        ordertype,
        lots,
        price,
        trigger,
        product,
        remarks,
    )

    # Ensure fresh contract files exist.
    logger.info("Downloading latest master contracts...")
    files = download_master_contracts(base_dir)
    review_msg = base_dir / "data" / "expiries_review.json"
    logger.info("Master contracts refreshed. Review expiries in %s", review_msg)

    freeze_lots = _load_freeze_lots(base_dir / "config.ini")
    max_lots = freeze_lots[instrument]
    chunks = _split_lots(lots, max_lots)
    logger.info("Freeze lots config: %s", freeze_lots)
    logger.info("Split plan for %s lots with max %s: %s", lots, max_lots, chunks)

    master_rows = _load_master_rows(
        files["NFO"].txt_path if instrument in {"NIFTY", "BANKNIFTY"} else files["BFO"].txt_path
    )
    contract = _resolve_contract(master_rows, instrument, expiry, strike, opt)
    tradingsymbol = contract["TradingSymbol"]
    lot_size = int(float(contract["LotSize"]))
    exchange = "NFO" if instrument in {"NIFTY", "BANKNIFTY"} else "BFO"

    logger.info(
        "Resolved contract: tsym=%s exchange=%s lot_size=%s chunks=%s",
        tradingsymbol,
        exchange,
        lot_size,
        chunks,
    )

    creds_path = base_dir / "creds.txt"
    creds = load_credentials(creds_path)
    api = FlattradeApi()
    logger.info("Starting login/session validation...")
    login_result = login_from_creds(api, creds, logger, creds_path=creds_path)
    logger.info("Login response: %s", login_result)
    # Re-read in case token got refreshed and saved during login_from_creds.
    creds = load_credentials(creds_path)
    session_token = str(creds.get("session_token", "")).strip()
    if not session_token:
        raise RuntimeError("session_token missing in creds.txt after login flow")
    logger.info("Using session token for raw PlaceOrder debug calls: %s", _mask_secret(session_token))

    validation = _validate_trading_session(
        base_host=broker_base,
        user_id=str(creds["username"]),
        account_id=str(creds["username"]),
        session_token=session_token,
        logger=logger,
    )
    if str(validation.get("stat", "")).lower() != "ok":
        emsg = validation.get("emsg", "Unknown session validation failure")
        raise RuntimeError(
            f"Trading session validation failed before order placement: {emsg}. "
            "Token exists but is not valid for PiConnectTP trading endpoints."
        )

    expects_enum = _method_expects_enum(api.place_order)
    buy_sell = _enum_or_wrapped(side, BuyorSell, expects_enum)
    product_type = _enum_or_wrapped(product, ProductType, expects_enum)
    price_type = _enum_or_wrapped(ordertype, PriceType, expects_enum)
    logger.info(
        "Enum adapter check: expects_enum=%s buy_sell=%s product=%s price_type=%s",
        expects_enum,
        getattr(buy_sell, "value", buy_sell),
        getattr(product_type, "value", product_type),
        getattr(price_type, "value", price_type),
    )

    placed = []
    for idx, lots in enumerate(chunks, start=1):
        qty_units = lots * lot_size
        order_price = price if ordertype == "LMT" else 0.0
        order_trigger = trigger if ordertype == "LMT" else None
        tag = f"{remarks}_{idx}of{len(chunks)}"

        logger.info("Placing child order %s/%s with lots=%s units=%s", idx, len(chunks), lots, qty_units)
        response = _place_order_raw_debug(
            base_host=broker_base,
            user_id=str(creds["username"]),
            session_token=session_token,
            side=getattr(buy_sell, "value", buy_sell),
            product=getattr(product_type, "value", product_type),
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            quantity=qty_units,
            price_type=getattr(price_type, "value", price_type),
            price=order_price,
            trigger_price=order_trigger,
            remarks=tag,
            logger=logger,
        )
        logger.info("Child order response: %s", response)
        placed.append(response)

    ok_count = sum(1 for item in placed if str(item.get("stat", "")).lower() == "ok")
    logger.info("Order placement summary: total=%s success=%s failed=%s", len(placed), ok_count, len(placed) - ok_count)
    print("Placed orders:")
    for item in placed:
        print(item)


if __name__ == "__main__":
    main()
