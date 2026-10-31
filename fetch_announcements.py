import csv
import json
import time
from pathlib import Path

import requests

ROOT = Path(__file__).parent
CAP_FILE = ROOT / "data" / "amfi_cap_list.csv"
OUT_FILE = ROOT / "docs" / "announcements.json"
MAX_KEEP = 1000

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
}


def fetch_nse():
    s = requests.Session()
    s.headers.update(HEADERS)
    s.get("https://www.nseindia.com", timeout=15)
    time.sleep(1)
    r = s.get(
        "https://www.nseindia.com/api/corporate-announcements",
        params={"index": "equities"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def load_caps():
    with open(CAP_FILE, encoding="utf-8") as f:
        return {r["Symbol"].strip().upper(): r["Category"].strip() for r in csv.DictReader(f)}


def main():
    caps = load_caps()
    old = json.loads(OUT_FILE.read_text(encoding="utf-8"))["items"] if OUT_FILE.exists() else []
    seen = {(i["symbol"], i["subject"], i["time"]) for i in old}

    new = []
    for a in fetch_nse():
        sym = (a.get("symbol") or "").upper()
        item = {
            "symbol": sym,
            "company": a.get("sm_name") or sym,
            "cap": caps.get(sym, "Other"),
            "subject": a.get("desc") or "",
            "details": (a.get("attchmntText") or "")[:600],
            "time": a.get("an_dt") or a.get("exchdisstime") or "",
            "pdf": a.get("attchmntFile") or "",
        }
        if (item["symbol"], item["subject"], item["time"]) not in seen:
            new.append(item)

    items = (new + old)[:MAX_KEEP]
    OUT_FILE.parent.mkdir(exist_ok=True)
    OUT_FILE.write_text(
        json.dumps({"updated": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()), "items": items},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"{len(new)} new, {len(items)} total")


if __name__ == "__main__":
    main()
