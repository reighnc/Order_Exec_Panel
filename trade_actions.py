import argparse
import base64
import hashlib
import hmac
import inspect
import json
import logging
import struct
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from NorenRestApiPy.NorenApi import NorenApi, BuyorSell, PriceType, ProductType


class _ValueWrapper:
    def __init__(self, value: str) -> None:
        self.value = value


class FlattradeApi(NorenApi):
    HOST_URL = "https://piconnect.flattrade.in/PiConnectAPI/"
    WS_URL = "wss://piconnect.flattrade.in/PiConnectWSAPI/"

    def __init__(self) -> None:
        super().__init__(
            host=self.HOST_URL,
            websocket=self.WS_URL,
        )


def setup_logger(base_dir: Path) -> logging.Logger:
    logs_dir = base_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    file_name = datetime.now().strftime("%Y%m%d") + ".txt"
    log_file = logs_dir / file_name

    logger = logging.getLogger("flattrade_trader")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    stream_handler = logging.StreamHandler()
    file_handler.setFormatter(formatter)
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    logger.info("Logging started. Log file: %s", log_file)
    return logger


def load_credentials(creds_path: Path) -> Dict[str, Any]:
    with creds_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_credentials(creds_path: Path, creds: Dict[str, Any]) -> None:
    with creds_path.open("w", encoding="utf-8") as f:
        json.dump(creds, f, indent=2)
        f.write("\n")


def generate_totp(base32_secret: str, interval: int = 30, digits: int = 6) -> str:
    secret = base32_secret.replace(" ", "").upper()
    pad_len = (8 - len(secret) % 8) % 8
    secret += "=" * pad_len
    key = base64.b32decode(secret, casefold=True)

    counter = int(time.time() // interval)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10**digits)).zfill(digits)


def resolve_twofa(twofa_value: Optional[str]) -> str:
    if not twofa_value:
        return ""
    twofa_value = str(twofa_value).strip()
    if len(twofa_value) == 6 and twofa_value.isdigit():
        return twofa_value
    try:
        return generate_totp(twofa_value)
    except Exception:
        return twofa_value


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 6:
        return "*" * len(value)
    return f"{value[:3]}***{value[-3:]}"


def _method_expects_enum(method: Any) -> bool:
    try:
        source = inspect.getsource(method)
    except Exception:
        return False
    return ".value" in source


def _enum_or_wrapped(raw_value: str, enum_cls: Any, expects_enum: bool) -> Any:
    if not expects_enum:
        return raw_value
    for member in enum_cls:
        if member.value == raw_value or member.name == raw_value:
            return member
    return _ValueWrapper(raw_value)


def login_from_creds(
    api: FlattradeApi,
    creds: Dict[str, Any],
    logger: logging.Logger,
    creds_path: Optional[Path] = None,
) -> Dict[str, Any]:
    logger.info(
        "TRADING_ENDPOINTS host=%s websocket=%s",
        FlattradeApi.HOST_URL,
        FlattradeApi.WS_URL,
    )
    username = str(creds.get("username", "")).strip()
    password = str(creds.get("password", "")).strip()

    if not username:
        raise ValueError("Missing 'username' in creds.txt")

    session_token = (
        creds.get("session_token")
        or creds.get("susertoken")
        or creds.get("usertoken")
    )

    if not session_token and creds_path is not None:
        try:
            from token_login import generate_session_token

            logger.info("No session token found. Generating a new token automatically.")
            token_data = generate_session_token(creds, logger)
            new_token = str(token_data.get("token", "")).strip()
            if not new_token:
                raise RuntimeError("Generated token is empty")

            creds["session_token"] = new_token
            creds["session_generated_at"] = datetime.now().isoformat(timespec="seconds")
            save_credentials(creds_path, creds)
            session_token = new_token
            logger.info("Generated session token (full): %s", new_token)
            logger.info("Generated and saved new session token.")
        except Exception as exc:
            logger.warning("Auto token generation (missing-token path) failed: %s", exc)

    if session_token:
        logger.info("Using session token from creds.")
        logger.info("Session token from creds (full): %s", session_token)
        result = api.set_session(userid=username, password="", usertoken=session_token)
        logger.info("set_session response (full): %s", result)
        if result:
            logger.info("Session set result: %s", result)
            return {"stat": "Ok", "mode": "set_session", "result": result}

        logger.warning("Existing session token is invalid/expired.")
        if creds_path is not None:
            try:
                from token_login import generate_session_token

                logger.info("Refreshing session token automatically (single retry).")
                token_data = generate_session_token(creds, logger)
                new_token = str(token_data.get("token", "")).strip()
                if not new_token:
                    raise RuntimeError("Generated token is empty")

                creds["session_token"] = new_token
                creds["session_generated_at"] = datetime.now().isoformat(timespec="seconds")
                save_credentials(creds_path, creds)
                logger.info("Refreshed session token (full): %s", new_token)

                retry_result = api.set_session(userid=username, password="", usertoken=new_token)
                logger.info("set_session retry response (full): %s", retry_result)
                if retry_result:
                    logger.info("Session refresh successful.")
                    return {"stat": "Ok", "mode": "set_session_refreshed", "result": retry_result}
                logger.warning("Session refresh retry failed; falling back to direct login.")
            except Exception as exc:
                logger.warning("Session auto-refresh failed: %s", exc)
        else:
            logger.warning("creds_path not provided; cannot auto-refresh session token.")

    if not password:
        raise ValueError("Missing 'password' in creds.txt for login flow.")

    twofa_totp = resolve_twofa(creds.get("2fa"))
    twofa_raw = str(creds.get("2fa", "")).strip()
    pan_value = str(creds.get("pan", "")).strip()
    vendor_code = str(creds.get("vendor_code") or creds.get("vc") or "").strip()
    api_key = str(creds.get("api_key", "")).strip()
    api_secret = str(creds.get("api_secret", "")).strip()
    imei = str(creds.get("imei", "abc1234")).strip()

    if not api_secret and not api_key:
        raise ValueError("Missing 'api_secret' and 'api_key' in creds.txt")
    if not (twofa_totp or twofa_raw or pan_value):
        raise ValueError("Missing valid '2fa' in creds.txt")

    twofa_candidates = []
    for value in (twofa_totp, twofa_raw, pan_value):
        if value and value not in twofa_candidates:
            twofa_candidates.append(value)

    vendor_candidates = []
    for value in (vendor_code, api_key, username):
        if value and value not in vendor_candidates:
            vendor_candidates.append(value)

    secret_candidates = []
    for value in (api_secret, api_key):
        if value and value not in secret_candidates:
            secret_candidates.append(value)

    last_error: Optional[str] = None
    for vc in vendor_candidates:
        for secret in secret_candidates:
            for factor2 in twofa_candidates:
                logger.info(
                    "Attempting login for user=%s with vc=%s and 2FA mode=%s",
                    username,
                    _mask_secret(vc),
                    "otp" if factor2 == twofa_totp else "raw/pan",
                )
                try:
                    response = api.login(
                        userid=username,
                        password=password,
                        twoFA=factor2,
                        vendor_code=vc,
                        api_secret=secret,
                        imei=imei,
                    )
                except Exception as exc:
                    last_error = str(exc)
                    logger.warning("Login call exception for vc=%s: %s", _mask_secret(vc), exc)
                    continue
                if response:
                    logger.info("Login successful. Full response: %s", response)
                    return response

    message = "Login failed. Add explicit vendor_code/vc/imei in creds.txt or use session_token."
    if last_error:
        message = f"{message} Last error: {last_error}"
    raise RuntimeError(message)


def place_limit_order(
    api: FlattradeApi,
    side: str,
    product: str,
    exchange: str,
    symbol: str,
    quantity: int,
    price: float,
    remarks: str,
) -> Dict[str, Any]:
    expects_enum = _method_expects_enum(api.place_order)
    return api.place_order(
        buy_or_sell=_enum_or_wrapped(side, BuyorSell, expects_enum),
        product_type=_enum_or_wrapped(product, ProductType, expects_enum),
        exchange=exchange,
        tradingsymbol=symbol,
        quantity=quantity,
        discloseqty=0,
        price_type=_enum_or_wrapped("LMT", PriceType, expects_enum),
        price=price,
        trigger_price=None,
        retention="DAY",
        remarks=remarks,
    )


def place_market_order(
    api: FlattradeApi,
    side: str,
    product: str,
    exchange: str,
    symbol: str,
    quantity: int,
    remarks: str,
) -> Dict[str, Any]:
    expects_enum = _method_expects_enum(api.place_order)
    return api.place_order(
        buy_or_sell=_enum_or_wrapped(side, BuyorSell, expects_enum),
        product_type=_enum_or_wrapped(product, ProductType, expects_enum),
        exchange=exchange,
        tradingsymbol=symbol,
        quantity=quantity,
        discloseqty=0,
        price_type=_enum_or_wrapped("MKT", PriceType, expects_enum),
        price=0,
        trigger_price=None,
        retention="DAY",
        remarks=remarks,
    )


def cancel_order(api: FlattradeApi, orderno: str) -> Dict[str, Any]:
    return api.cancel_order(orderno=orderno)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Flattrade order actions.")
    parser.add_argument(
        "--creds",
        default="creds.txt",
        help="Path to creds JSON file (default: creds.txt)",
    )

    subparsers = parser.add_subparsers(dest="command")

    limit_cmd = subparsers.add_parser("limit", help="Place a limit order")
    limit_cmd.add_argument("--side", choices=["B", "S"], required=True)
    limit_cmd.add_argument("--product", default="C", help="Product type (default: C)")
    limit_cmd.add_argument("--exchange", default="NSE")
    limit_cmd.add_argument("--symbol", required=True, help="Trading symbol like INFY-EQ")
    limit_cmd.add_argument("--qty", type=int, required=True)
    limit_cmd.add_argument("--price", type=float, required=True)
    limit_cmd.add_argument("--remarks", default="limit_order")

    market_cmd = subparsers.add_parser("market", help="Place a market order")
    market_cmd.add_argument("--side", choices=["B", "S"], required=True)
    market_cmd.add_argument("--product", default="C", help="Product type (default: C)")
    market_cmd.add_argument("--exchange", default="NSE")
    market_cmd.add_argument("--symbol", required=True, help="Trading symbol like INFY-EQ")
    market_cmd.add_argument("--qty", type=int, required=True)
    market_cmd.add_argument("--remarks", default="market_order")

    cancel_cmd = subparsers.add_parser("cancel", help="Cancel an existing order")
    cancel_cmd.add_argument("--orderno", required=True, help="Noren order number")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    base_dir = Path(__file__).resolve().parent
    logger = setup_logger(base_dir)

    creds_path = Path(args.creds)
    if not creds_path.is_absolute():
        creds_path = base_dir / creds_path

    try:
        creds = load_credentials(creds_path)
        api = FlattradeApi()
        login_result = login_from_creds(api, creds, logger, creds_path=creds_path)
        logger.info("Login response: %s", login_result)

        if args.command == "limit":
            result = place_limit_order(
                api=api,
                side=args.side,
                product=args.product,
                exchange=args.exchange,
                symbol=args.symbol,
                quantity=args.qty,
                price=args.price,
                remarks=args.remarks,
            )
            logger.info("Limit order result: %s", result)
            print(result)
        elif args.command == "market":
            result = place_market_order(
                api=api,
                side=args.side,
                product=args.product,
                exchange=args.exchange,
                symbol=args.symbol,
                quantity=args.qty,
                remarks=args.remarks,
            )
            logger.info("Market order result: %s", result)
            print(result)
        elif args.command == "cancel":
            result = cancel_order(api=api, orderno=args.orderno)
            logger.info("Cancel order result: %s", result)
            print(result)
        else:
            logger.info("Login completed. No order command provided.")
            print("Logged in successfully. Use limit/market/cancel commands to execute actions.")

    except Exception as exc:
        logger.exception("Execution failed: %s", exc)
        raise


if __name__ == "__main__":
    main()
