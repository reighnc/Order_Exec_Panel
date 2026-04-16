import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

import requests

from trade_actions import FlattradeApi, load_credentials, resolve_twofa, setup_logger


AUTH_API_BASE = "https://authapi.flattrade.in"
SESSION_URL = f"{AUTH_API_BASE}/auth/session"
FTAUTH_URL = f"{AUTH_API_BASE}/ftauth"
APITOKEN_URL = f"{AUTH_API_BASE}/trade/apitoken"


def _extract_request_code(redirect_url: str) -> str:
    parsed = urlparse(redirect_url)
    raw_query = (parsed.query or "").lstrip("?")
    query = parse_qs(raw_query)
    for key in ("request_code", "request_token", "code", "?request_code", "?request_token", "?code"):
        value = query.get(key)
        if value and value[0]:
            return str(value[0]).strip()

    # Fallback for non-standard redirect query formatting.
    match = re.search(r"(?:[?&]|^)(?:request_code|request_token|code)=([^&]+)", redirect_url)
    if match:
        return match.group(1).strip()
    return ""


def _compute_security_hash(api_key: str, request_code: str, api_secret: str) -> str:
    # Per Flattrade docs: SHA256(api_key + request_code + api_secret)
    raw = f"{api_key}{request_code}{api_secret}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _save_session_token(creds_path: Path, creds: Dict[str, Any], token: str) -> None:
    creds["session_token"] = token
    creds["session_generated_at"] = datetime.now().isoformat(timespec="seconds")
    with creds_path.open("w", encoding="utf-8") as f:
        json.dump(creds, f, indent=2)
        f.write("\n")


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 6:
        return "*" * len(value)
    return f"{value[:3]}***{value[-3:]}"


def generate_session_token(creds: Dict[str, Any], logger: Any) -> Dict[str, Any]:
    username = str(creds.get("username", "")).strip()
    password = str(creds.get("password", "")).strip()
    api_key = str(creds.get("api_key", "")).strip()
    api_secret = str(creds.get("api_secret", "")).strip()
    twofa = resolve_twofa(creds.get("2fa"))

    if not all([username, password, api_key, api_secret, twofa]):
        raise ValueError("Missing one of username/password/api_key/api_secret/2fa in creds.txt")

    # authapi endpoints enforce origin/referer checks similar to browser calls.
    auth_headers = {
        "Origin": "https://auth.flattrade.in",
        "Referer": f"https://auth.flattrade.in/?app_key={api_key}",
        "User-Agent": "Mozilla/5.0",
    }
    logger.info(
        "AUTH_ENDPOINTS auth_base=%s session_url=%s ftauth_url=%s apitoken_url=%s",
        AUTH_API_BASE,
        SESSION_URL,
        FTAUTH_URL,
        APITOKEN_URL,
    )
    logger.info("AUTH_REFERER %s", auth_headers["Referer"])

    logger.info("Step 1/4: requesting auth session id")
    sid_resp = requests.post(SESSION_URL, headers=auth_headers, timeout=20)
    sid_resp.raise_for_status()
    sid = sid_resp.text.strip()
    logger.info("AUTH_RESPONSE /auth/session status=%s body=%s", sid_resp.status_code, sid_resp.text)
    if not sid:
        raise RuntimeError("Empty SID from auth/session")
    logger.info("Received SID (full): %s", sid)

    logger.info("Step 2/4: authenticating at /ftauth")
    login_payload = {
        "UserName": username,
        "Password": hashlib.sha256(password.encode("utf-8")).hexdigest(),
        "PAN_DOB": twofa.upper(),
        "App": "",
        "ClientID": "",
        "Key": "",
        "APIKey": api_key,
        "Sid": sid,
        "Override": "",
        "Source": "AUTHPAGE",
        "Rd": "",
    }
    logger.info("AUTH_REQUEST /ftauth payload=%s", login_payload)
    login_resp = requests.post(FTAUTH_URL, json=login_payload, headers=auth_headers, timeout=20)
    login_resp.raise_for_status()
    login_data = login_resp.json()
    logger.info("AUTH_RESPONSE /ftauth status=%s body=%s", login_resp.status_code, login_data)
    if login_data.get("emsg"):
        raise RuntimeError(f"/ftauth failed: {login_data.get('emsg')}")

    redirect_url = str(login_data.get("RedirectURL", "")).strip()
    if not redirect_url:
        raise RuntimeError("No RedirectURL returned by /ftauth")
    logger.info("Received RedirectURL (full): %s", redirect_url)

    logger.info("Step 3/4: extracting request code")
    request_code = _extract_request_code(redirect_url)
    if not request_code:
        raise RuntimeError("Could not find request_code/request_token in RedirectURL")
    logger.info("Extracted request_code (full): %s", request_code)

    logger.info("Step 4/4: exchanging request code for API token")
    sec_hash = _compute_security_hash(api_key, request_code, api_secret)
    token_payload = {
        "api_key": api_key,
        "request_code": request_code,
        "api_secret": sec_hash,
    }
    logger.info("AUTH_REQUEST /trade/apitoken payload=%s", token_payload)
    token_resp = requests.post(APITOKEN_URL, json=token_payload, timeout=20)
    token_resp.raise_for_status()
    token_data = token_resp.json()
    logger.info("AUTH_RESPONSE /trade/apitoken status=%s body=%s", token_resp.status_code, token_data)
    token_stat = str(token_data.get("status", token_data.get("stat", ""))).lower()
    if token_stat != "ok":
        raise RuntimeError(f"/trade/apitoken failed: {token_data}")

    token = str(token_data.get("token", "")).strip()
    if not token:
        raise RuntimeError("Token missing in /trade/apitoken response")
    logger.info("Generated API token (full): %s", token)
    return token_data


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Auto-generate Flattrade session token and save to creds.txt"
    )
    parser.add_argument("--creds", default="creds.txt", help="Path to creds JSON")
    parser.add_argument(
        "--verify-session",
        action="store_true",
        help="After token generation, verify set_session against trading API",
    )
    args = parser.parse_args()

    base_dir = Path(__file__).resolve().parent
    logger = setup_logger(base_dir)

    creds_path = Path(args.creds)
    if not creds_path.is_absolute():
        creds_path = base_dir / creds_path

    creds = load_credentials(creds_path)
    token_data = generate_session_token(creds, logger)
    token = str(token_data["token"])
    _save_session_token(creds_path, creds, token)
    logger.info("Saved session token to creds (full): %s", token)
    logger.info("Session token generated and saved to creds.txt")
    print("TOKEN GENERATED AND SAVED")

    if args.verify_session:
        api = FlattradeApi()
        result = api.set_session(userid=str(creds.get("username", "")).strip(), password="", usertoken=token)
        logger.info("set_session result: %s", result)
        print("SESSION VERIFY RESPONSE:", result)


if __name__ == "__main__":
    main()
