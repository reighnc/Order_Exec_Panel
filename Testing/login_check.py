import argparse
from pathlib import Path

from trade_actions import FlattradeApi, load_credentials, login_from_creds, setup_logger


def main() -> None:
    parser = argparse.ArgumentParser(description="Flattrade login-only check.")
    parser.add_argument(
        "--creds",
        default="creds.txt",
        help="Path to creds JSON file (default: creds.txt)",
    )
    args = parser.parse_args()

    base_dir = Path(__file__).resolve().parent
    logger = setup_logger(base_dir)

    creds_path = Path(args.creds)
    if not creds_path.is_absolute():
        creds_path = base_dir / creds_path

    try:
        creds = load_credentials(creds_path)
        api = FlattradeApi()
        result = login_from_creds(api, creds, logger)
        logger.info("Login check successful: %s", result)
        print("LOGIN OK")
    except Exception as exc:
        logger.exception("Login check failed: %s", exc)
        print("LOGIN FAILED")
        raise


if __name__ == "__main__":
    main()
