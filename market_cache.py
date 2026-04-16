import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

import requests

from trade_actions import FlattradeApi


INSTRUMENTS = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX")

UI_EXCHANGE = {
    "NIFTY": "NSE",
    "BANKNIFTY": "NSE",
    "FINNIFTY": "NSE",
    "MIDCPNIFTY": "NSE",
    "SENSEX": "BFO",
}

OPTION_EXCHANGE = {
    "NIFTY": "NFO",
    "BANKNIFTY": "NFO",
    "FINNIFTY": "NFO",
    "MIDCPNIFTY": "NFO",
    "SENSEX": "BFO",
}

SPOT_TOKEN_MAP = {
    "NIFTY": ("NSE", "26000"),
    "BANKNIFTY": ("NSE", "26009"),
}

PUBLIC_INDEX_NAME_MAP = {
    "NIFTY": "NIFTY 50",
    "BANKNIFTY": "NIFTY BANK",
    "FINNIFTY": "NIFTY FINANCIAL SERVICES",
    "MIDCPNIFTY": "NIFTY MIDCAP SELECT",
}


@dataclass
class InstrumentCache:
    expiries: List[str]
    strikes_by_expiry: Dict[str, List[int]]
    spot_ltp: Optional[float]


_PATTERNS = (
    re.compile(r"^(NIFTY|BANKNIFTY|FINNIFTY|MIDCPNIFTY)(\d{2}[A-Z]{3}\d{2})([CP])(\d+)$"),
    re.compile(r"^(SENSEX)(\d{2}[A-Z]{3}\d{2})([CP])(\d+)$"),
    re.compile(r"^(NIFTY|BANKNIFTY|FINNIFTY|MIDCPNIFTY|SENSEX)(\d{2}[A-Z]{3}\d{2})(\d+)(CE|PE)$"),
)


def _expiry_sort_key(expiry: str) -> Tuple[int, str]:
    try:
        return (0, datetime.strptime(expiry, "%d%b%y").strftime("%Y%m%d"))
    except ValueError:
        return (1, expiry)


def _parse_tsym(instrument: str, tsym: str) -> Optional[Tuple[str, int]]:
    if instrument not in tsym:
        return None
    clean = tsym.replace("-", "")
    for pat in _PATTERNS:
        m = pat.match(clean)
        if not m:
            continue
        # Variant 1/2: (...) (expiry) (C/P) (strike)
        if len(m.groups()) == 4 and m.group(3) in {"C", "P"}:
            return (m.group(2), int(m.group(4)))
        # Variant 3: (...) (expiry) (strike) (CE/PE)
        if len(m.groups()) == 4 and m.group(4) in {"CE", "PE"}:
            return (m.group(2), int(m.group(3)))
    return None


def _safe_float(value: object) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _public_index_spot_map() -> Dict[str, float]:
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://www.nseindia.com/market-data/live-equity-market",
    }
    with requests.Session() as session:
        session.get("https://www.nseindia.com", headers=headers, timeout=15)
        response = session.get("https://www.nseindia.com/api/allIndices", headers=headers, timeout=20)
    if response.status_code != 200:
        return {}
    payload = response.json()
    rows = payload.get("data", [])
    by_name = {str(row.get("index", "")).upper(): row for row in rows}

    result: Dict[str, float] = {}
    for instrument, index_name in PUBLIC_INDEX_NAME_MAP.items():
        row = by_name.get(index_name.upper())
        if not row:
            continue
        value = _safe_float(row.get("last"))
        if value is not None:
            result[instrument] = value
    return result


def _search_token(api: FlattradeApi, exchange: str, search_text: str) -> Optional[str]:
    try:
        resp = api.searchscrip(exchange=exchange, searchtext=search_text)
    except Exception:
        return None
    if not resp or "values" not in resp:
        return None
    for row in resp["values"]:
        tsym = str(row.get("tsym", "")).upper()
        if tsym == search_text.upper():
            return row.get("token")
    first = resp["values"][0] if resp["values"] else {}
    return first.get("token")


def _safe_get_quote(api: FlattradeApi, exchange: str, token: str) -> Optional[dict]:
    try:
        quote = api.get_quotes(exchange=exchange, token=token)
    except Exception:
        return None
    if isinstance(quote, dict):
        return quote
    return None


def get_spot_ltp(api: FlattradeApi, instrument: str) -> Optional[float]:
    if instrument in SPOT_TOKEN_MAP:
        exch, token = SPOT_TOKEN_MAP[instrument]
        quote = _safe_get_quote(api, exch, token)
        if quote:
            value = _safe_float(quote.get("lp"))
            if value is not None:
                return value
    elif instrument == "SENSEX":
        token = _search_token(api, "BSE", "SENSEX")
        exch = "BSE"
        if not token:
            exch = None
        else:
            quote = _safe_get_quote(api, exch, token)
            if quote:
                value = _safe_float(quote.get("lp"))
                if value is not None:
                    return value

    # Trading quote endpoint may fail for some sessions; fallback to NSE public index LTP.
    public_spots = _public_index_spot_map()
    if instrument in public_spots:
        return public_spots[instrument]
    return None


def _fallback_strikes(instrument: str, spot_ltp: Optional[float]) -> List[int]:
    step_map = {"NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50, "MIDCPNIFTY": 25, "SENSEX": 100}
    default_base_map = {"NIFTY": 22000, "BANKNIFTY": 48000, "FINNIFTY": 22000, "MIDCPNIFTY": 11000, "SENSEX": 75000}
    step = step_map.get(instrument, 100)
    base = default_base_map.get(instrument, 22000)
    if spot_ltp:
        base = int(round(spot_ltp / step) * step)
    return [base + i * step for i in range(-40, 41)]


def _fallback_expiries(instrument: str, count: int = 12) -> List[str]:
    # NIFTY/BANKNIFTY weekly expiry: Thursday, SENSEX weekly expiry: Friday.
    weekday = 3 if instrument in {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"} else 4
    today = date.today()
    result: List[str] = []
    cursor = today
    while len(result) < count:
        if cursor.weekday() == weekday and cursor >= today:
            result.append(cursor.strftime("%d%b%y").upper())
        cursor += timedelta(days=1)
    return result


def _nse_option_chain(symbol: str) -> Optional[InstrumentCache]:
    # NSE blocks simple bot-like requests. Warm up with a landing page call and keep session cookies.
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://www.nseindia.com/option-chain",
    }
    with requests.Session() as session:
        session.get("https://www.nseindia.com/option-chain", headers=headers, timeout=15)
        response = session.get(
            f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}",
            headers=headers,
            timeout=20,
        )
    if response.status_code != 200:
        return None
    payload = response.json()
    records = payload.get("records", {})
    data = records.get("data", [])
    expiry_list = records.get("expiryDates", [])
    spot = _safe_float(records.get("underlyingValue"))
    if not expiry_list:
        return None

    strikes_by_expiry: Dict[str, List[int]] = {}
    allowed = set(expiry_list[:12])  # roughly 2-3 months
    for row in data:
        expiry = row.get("expiryDate")
        strike = row.get("strikePrice")
        if expiry not in allowed:
            continue
        strike_val = _safe_float(strike)
        if strike_val is None:
            continue
        strikes_by_expiry.setdefault(expiry.upper(), []).append(int(strike_val))

    # Normalize order + unique
    normalized_expiries = [exp.upper() for exp in expiry_list[:12] if exp]
    for expiry in list(strikes_by_expiry.keys()):
        strikes_by_expiry[expiry] = sorted(set(strikes_by_expiry[expiry]))
    normalized_expiries = [exp for exp in normalized_expiries if exp in strikes_by_expiry]
    if not normalized_expiries:
        return None

    return InstrumentCache(
        expiries=normalized_expiries,
        strikes_by_expiry=strikes_by_expiry,
        spot_ltp=spot,
    )


def fetch_instrument_cache(api: FlattradeApi, instrument: str) -> InstrumentCache:
    # Prefer NSE public option chain for richer, reliable expiry/strike ladders on index symbols.
    if instrument in {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"}:
        nse_chain = _nse_option_chain(instrument)
        if nse_chain:
            return nse_chain

    option_exchange = OPTION_EXCHANGE[instrument]
    resp = api.searchscrip(exchange=option_exchange, searchtext=instrument)

    strikes_by_expiry: Dict[str, List[int]] = {}
    if resp and "values" in resp:
        for row in resp["values"]:
            tsym = str(row.get("tsym", "")).upper()
            parsed = _parse_tsym(instrument, tsym)
            if not parsed:
                continue
            expiry, strike = parsed
            strikes_by_expiry.setdefault(expiry, []).append(strike)

    for expiry in list(strikes_by_expiry.keys()):
        strikes_by_expiry[expiry] = sorted(set(strikes_by_expiry[expiry]))

    expiries = sorted(strikes_by_expiry.keys(), key=_expiry_sort_key)
    spot_ltp = get_spot_ltp(api, instrument)

    if not expiries:
        expiries = _fallback_expiries(instrument, count=12)
        fallback = _fallback_strikes(instrument, spot_ltp)
        for expiry in expiries:
            strikes_by_expiry[expiry] = fallback

    return InstrumentCache(expiries=expiries, strikes_by_expiry=strikes_by_expiry, spot_ltp=spot_ltp)


def nearest_atm_strike(strikes: List[int], spot_ltp: Optional[float]) -> Optional[int]:
    if not strikes:
        return None
    if spot_ltp is None:
        return strikes[len(strikes) // 2]
    return min(strikes, key=lambda value: abs(value - spot_ltp))


def strike_window_around_atm(strikes: List[int], spot_ltp: Optional[float], width_each_side: int = 30) -> List[int]:
    if not strikes:
        return []
    atm = nearest_atm_strike(strikes, spot_ltp)
    if atm is None:
        return strikes
    idx = strikes.index(atm)
    start = max(0, idx - width_each_side)
    end = min(len(strikes), idx + width_each_side + 1)
    return strikes[start:end]
