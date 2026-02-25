import csv
import io
import json
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List

import requests


DOWNLOAD_URLS = {
    "NFO": "https://api.shoonya.com/NFO_symbols.txt.zip",
    "BFO": "https://api.shoonya.com/BFO_symbols.txt.zip",
}


@dataclass
class ContractFiles:
    zip_path: Path
    txt_path: Path


def _download_zip(url: str, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    out_path.write_bytes(response.content)


def _extract_txt(zip_path: Path, out_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "r") as archive:
        first = archive.namelist()[0]
        text = archive.read(first)
    out_path.write_bytes(text)


def download_master_contracts(base_dir: Path) -> Dict[str, ContractFiles]:
    output: Dict[str, ContractFiles] = {}
    master_dir = base_dir / "data" / "master"
    for exch, url in DOWNLOAD_URLS.items():
        zip_path = master_dir / f"{exch}_symbols.zip"
        txt_path = master_dir / f"{exch}_symbols.txt"
        _download_zip(url, zip_path)
        _extract_txt(zip_path, txt_path)
        output[exch] = ContractFiles(zip_path=zip_path, txt_path=txt_path)
    return output


def _parse_expiry(expiry_text: str) -> datetime:
    return datetime.strptime(expiry_text, "%d-%b-%Y")


def _sorted_unique_expiries(rows: Iterable[Dict[str, str]]) -> List[str]:
    values = {row["Expiry"] for row in rows if row.get("Expiry")}
    return sorted(values, key=_parse_expiry)


def _load_rows(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def build_expiry_review(files: Dict[str, ContractFiles], base_dir: Path) -> Path:
    nfo_rows = _load_rows(files["NFO"].txt_path)
    bfo_rows = _load_rows(files["BFO"].txt_path)

    nifty_rows = [
        r for r in nfo_rows
        if r.get("Instrument") == "OPTIDX" and r.get("Symbol") == "NIFTY"
    ]
    banknifty_rows = [
        r for r in nfo_rows
        if r.get("Instrument") == "OPTIDX" and r.get("Symbol") == "BANKNIFTY"
    ]
    sensex_rows = [
        r for r in bfo_rows
        if r.get("Instrument") == "OPTIDX"
        and str(r.get("TradingSymbol", "")).startswith(("SENSEX", "SENSEX50"))
    ]

    review = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "instruments": {
            "NIFTY": _sorted_unique_expiries(nifty_rows),
            "BANKNIFTY": _sorted_unique_expiries(banknifty_rows),
            "SENSEX": _sorted_unique_expiries(sensex_rows),
        },
    }

    out_path = base_dir / "data" / "expiries_review.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(review, indent=2), encoding="utf-8")

    # Also write plain-text lists for quick manual review.
    for instrument, expiries in review["instruments"].items():
        txt_path = base_dir / "data" / f"expiries_{instrument.lower()}.txt"
        txt_path.write_text("\n".join(expiries) + ("\n" if expiries else ""), encoding="utf-8")
    return out_path


def main() -> None:
    base_dir = Path(__file__).resolve().parent
    files = download_master_contracts(base_dir)
    review_path = build_expiry_review(files, base_dir)

    print("Master contracts downloaded:")
    for exch, item in files.items():
        print(f"- {exch}: {item.txt_path}")
    print(f"Expiry review file: {review_path}")


if __name__ == "__main__":
    main()
