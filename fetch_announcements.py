import csv
import io
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

try:
    import pypdf
except ImportError:
    pypdf = None

ROOT = Path(__file__).parent
CAP_FILE = ROOT / "data" / "amfi_cap_list.csv"
ANN_FILE = ROOT / "docs" / "announcements.json"
EVT_FILE = ROOT / "docs" / "events.json"
ARCHIVE = ROOT / "docs" / "archive"

IST = timezone(timedelta(hours=5, minutes=30))
ARCHIVE_DAYS = 30
MAX_PDFS_PER_RUN = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
}
API = "https://www.nseindia.com/api"


# ---------- time helpers ----------
def to_ts(time_str):
    for fmt in ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(time_str, fmt).replace(tzinfo=IST)
        except ValueError:
            pass
    return None


def last_close(ref):
    c = ref.replace(hour=15, minute=30, second=0, microsecond=0)
    if ref < c:
        c -= timedelta(days=1)
    while c.weekday() >= 5:
        c -= timedelta(days=1)
    return c


def retention_cutoff(now):
    # keep everything after the second-most-recent market close (weekdays 15:30 IST)
    return last_close(last_close(now) - timedelta(seconds=1))


def dmy(d):
    return d.strftime("%d-%m-%Y")


# ---------- NSE ----------
def session():
    s = requests.Session()
    s.headers.update(HEADERS)
    s.get("https://www.nseindia.com", timeout=15)
    time.sleep(1)
    return s


def get_json(s, path, params=None, timeout=40):
    r = s.get(f"{API}/{path}", params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


# ---------- highlights (rule based, no AI) ----------
TAG_RULES = [
    ("ORDER", r"order (?:worth|valued?|of|for|win)|orders? (?:worth|valued?)|secur(?:es|ed|ing) (?:an? )?(?:new )?orders?|"
              r"receipt of (?:an? )?(?:new )?orders?|bagg(?:s|ed|ing)|letter of award|letter of intent|\bloi\b|"
              r"awarded|new orders?|contract (?:worth|valued?|win)|wins? (?:an? )?(?:order|contract|project)"),
    ("RESULTS", r"financial results?|unaudited|audited results?|quarter(?:ly)? results?"),
    ("DIVIDEND", r"dividend|\bbonus\b|stock split|sub-?division of|buy-?back"),
    ("M&A", r"acqui(?:re|res|red|sition)|merger|amalgamation|demerger|takeover|open offer|stake (?:in|sale)|sale of stake"),
    ("RATING", r"credit rating|rating (?:action|upgrade|downgrade|reaffirm)|upgraded|downgraded"),
    ("MGMT", r"resign|cessation|appointment of (?:ceo|cfo|managing|chief|whole)|\bceo\b|\bcfo\b|managing director"),
    ("FUNDS", r"fund[- ]?raising|\bqip\b|preferential (?:issue|allot)|rights issue|\bncds?\b|debentures"),
    ("LEGAL", r"sebi (?:order|penalty|notice)|order (?:passed )?by sebi|adjudicating officer|show cause|"
              r"penalty (?:of|imposed)|investigation|fraud|\bfir\b|tax demand|search and seizure|\braid\b|"
              r"enforcement directorate|\bcbi\b"),
]
PDF_TAGS = {"ORDER", "M&A", "RATING", "DIVIDEND", "RESULTS"}
THIN_SUBJECT = re.compile(r"^(?:general )?updates?$|press release|investor presentation", re.I)
ROUTINE = re.compile(
    r"newspaper publication|trading window|shareholding pattern|certificate under|"
    r"analyst|institutional investor|loss of share certificate|reg\.? ?74|reg(?:ulation)? 39|"
    r"closure of trading|compliance certificate|postal ballot",
    re.I,
)
AMT_RE = re.compile(
    r"(?:₹|rs\.?|inr)\s*([\d,]+(?:\.\d+)?)\s*(crores?|cr\b|lakhs?|million|mn|billion|bn)",
    re.I,
)
UNIT_TO_CR = {"crore": 1, "crores": 1, "cr": 1, "lakh": 0.01, "lakhs": 0.01,
              "million": 0.1, "mn": 0.1, "billion": 100, "bn": 100}


def amounts(text):
    best = None
    for num, unit in AMT_RE.findall(text):
        try:
            cr = float(num.replace(",", "")) * UNIT_TO_CR[unit.lower()]
        except ValueError:
            continue
        if best is None or cr > best[0]:
            best = (cr, f"₹{num} {unit.lower()}")
    return best


def find_tags(text):
    low = re.sub(r"substantial acquisition of shares(?: and takeovers?)?", "", text.lower())
    return [t for t, pat in TAG_RULES if re.search(pat, low)]


def enrich(item, extra_text=""):
    base = f"{item['subject']}. {item['details']}"
    tags = find_tags(base)
    if extra_text and (THIN_SUBJECT.search(item["subject"]) or not tags):
        for t in find_tags(extra_text[:2500]):
            if t in PDF_TAGS and t not in tags:
                tags.append(t)
    if ROUTINE.search(item["subject"]):
        tags = [t for t in tags if t == "LEGAL"]
    text = f"{base}\n{extra_text}"
    sentences = re.split(r"(?<=[.!?])\s+|\n+", re.sub(r"[ \t]+", " ", text))
    patterns = [p for t, p in TAG_RULES if t in tags]
    hl = []
    for sn in sentences:
        sn = sn.strip()
        if len(sn) < 25 or not re.search(r"\d", sn):
            continue
        if any(re.search(p, sn.lower()) for p in patterns):
            hl.append(sn[:260])
        if len(hl) == 2:
            break
    amt = amounts(" ".join(hl)) if hl else None
    item["tags"] = tags
    item["amt"] = amt[1] if amt else ""
    item["amt_cr"] = round(amt[0], 2) if amt else 0
    item["hl"] = hl
    item["hot"] = bool(
        "M&A" in tags or "LEGAL" in tags or ("ORDER" in tags and item["amt_cr"] >= 500)
    )
    return item


PDF_SUBJECTS = re.compile(
    r"result|outcome|press release|acquisition|award|order|contract|rating|"
    r"general updates|fund|dividend|merger|amalgamation|demerger|investor presentation|"
    r"updates",
    re.I,
)


def pdf_text(s, url, max_chars=4000):
    r = s.get(url, timeout=20)
    r.raise_for_status()
    reader = pypdf.PdfReader(io.BytesIO(r.content))
    out = "\n".join((p.extract_text() or "") for p in reader.pages[:3])
    return out.strip()[:max_chars]


def normalise(a, caps):
    sym = (a.get("symbol") or "").upper()
    return {
        "id": str(a.get("seq_id") or ""),
        "symbol": sym,
        "company": a.get("sm_name") or sym,
        "cap": caps.get(sym, "Other"),
        "subject": a.get("desc") or "",
        "details": (a.get("attchmntText") or "")[:700],
        "time": a.get("an_dt") or a.get("exchdisstime") or "",
        "pdf": a.get("attchmntFile") or "",
    }


def key(i):
    return i["id"] if i.get("id") else (i["symbol"], i["subject"], i["time"])


def process_new(s, new, allow_pdf, stats):
    for it in new:
        extra = ""
        if (allow_pdf and pypdf and it["cap"] != "Other" and it["pdf"].lower().endswith(".pdf")
                and PDF_SUBJECTS.search(it["subject"]) and stats["pdf_try"] < MAX_PDFS_PER_RUN):
            stats["pdf_try"] += 1
            try:
                extra = pdf_text(s, it["pdf"])
                stats["pdf_ok"] += 1
            except Exception:
                stats["pdf_fail"] += 1
        enrich(it, extra)


# ---------- events ----------
def iso_date(d):
    try:
        return datetime.strptime(d, "%d-%b-%Y").date().isoformat()
    except (ValueError, TypeError):
        return ""


def build_events(s, caps, now):
    events = {}
    for e in get_json(s, "event-calendar"):
        sym = (e.get("symbol") or "").upper()
        d = iso_date(e.get("date"))
        if not d:
            continue
        events[(sym, d, e.get("purpose", ""))] = {
            "symbol": sym, "company": e.get("company") or sym, "cap": caps.get(sym, "Other"),
            "purpose": e.get("purpose") or "", "desc": (e.get("bm_desc") or "")[:400], "date": d,
        }
    try:
        for e in get_json(s, "corporate-board-meetings", {"index": "equities"}):
            sym = (e.get("bm_symbol") or "").upper()
            d = iso_date(e.get("bm_date"))
            if not d:
                continue
            k = (sym, d, e.get("bm_purpose", ""))
            if any(kk[0] == sym and kk[1] == d for kk in events):
                continue
            events[k] = {
                "symbol": sym, "company": e.get("sm_name") or sym, "cap": caps.get(sym, "Other"),
                "purpose": e.get("bm_purpose") or "", "desc": (e.get("bm_desc") or "")[:400], "date": d,
            }
    except Exception:
        pass
    keep_from = (now.date() - timedelta(days=2)).isoformat()
    old = json.loads(EVT_FILE.read_text(encoding="utf-8"))["items"] if EVT_FILE.exists() else []
    for e in old:
        events.setdefault((e["symbol"], e["date"], e["purpose"]), e)
    items = [e for e in events.values() if e["date"] >= keep_from]
    items.sort(key=lambda e: (e["date"], e["symbol"]))
    return items


# ---------- main ----------
def load_caps():
    with open(CAP_FILE, encoding="utf-8") as f:
        return {r["Symbol"].strip().upper(): r["Category"].strip() for r in csv.DictReader(f)}


def write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def stamp():
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())


def day_path(d):
    return ARCHIVE / f"{d.isoformat()}.json"


def load_day(d):
    p = day_path(d)
    return json.loads(p.read_text(encoding="utf-8"))["items"] if p.exists() else []


def save_day(d, items):
    items.sort(key=lambda i: i.get("ts", ""), reverse=True)
    write(day_path(d), {"updated": stamp(), "items": items})


def fetch_range(s, d1, d2):
    return get_json(s, "corporate-announcements",
                    {"index": "equities", "from_date": dmy(d1), "to_date": dmy(d2)})


def main():
    now = datetime.now(IST)
    today = now.date()
    caps = load_caps()
    s = session()
    stats = {"pdf_try": 0, "pdf_ok": 0, "pdf_fail": 0, "new": 0, "skipped": 0, "backfilled_days": 0}

    cutoff = retention_cutoff(now)
    recent_from = cutoff.date()

    # 1) recent days: one range call, split by day, only unseen filings get processed
    by_day = {}
    for a in fetch_range(s, recent_from, today):
        it = normalise(a, caps)
        ts = to_ts(it["time"])
        it["ts"] = ts.isoformat() if ts else ""
        by_day.setdefault(ts.date() if ts else today, []).append(it)

    d = recent_from
    while d <= today:
        existing = load_day(d)
        seen = {key(i) for i in existing}
        new = []
        for it in by_day.get(d, []):
            if key(it) in seen:
                stats["skipped"] += 1
                continue
            seen.add(key(it))
            new.append(it)
        in_window = [i for i in new if i["ts"] and datetime.fromisoformat(i["ts"]) >= cutoff]
        process_new(s, in_window, True, stats)
        process_new(s, [i for i in new if "tags" not in i], False, stats)
        stats["new"] += len(new)
        save_day(d, existing + new)
        d += timedelta(days=1)

    # 2) backfill older days of the archive window (once per day, text-only highlights)
    start = today - timedelta(days=ARCHIVE_DAYS)
    d = start
    while d < recent_from:
        if not day_path(d).exists():
            try:
                rows = fetch_range(s, d, d)
            except Exception:
                d += timedelta(days=1)
                continue
            items, seen = [], set()
            for a in rows:
                it = normalise(a, caps)
                ts = to_ts(it["time"])
                if ts is None or ts.date() != d or key(it) in seen:
                    continue
                seen.add(key(it))
                it["ts"] = ts.isoformat()
                items.append(it)
            process_new(s, items, False, stats)
            save_day(d, items)
            stats["backfilled_days"] += 1
            time.sleep(0.7)
        d += timedelta(days=1)

    # 3) prune, index, and the small "recent" file the main page loads
    for p in ARCHIVE.glob("*.json"):
        if p.stem == "index":
            continue
        try:
            if datetime.strptime(p.stem, "%Y-%m-%d").date() < start:
                p.unlink()
        except ValueError:
            pass
    days = sorted(p.stem for p in ARCHIVE.glob("????-??-??.json"))
    write(ARCHIVE / "index.json", {"updated": stamp(), "days": days})

    recent = []
    d = recent_from
    while d <= today:
        recent += [i for i in load_day(d) if i["ts"] and datetime.fromisoformat(i["ts"]) >= cutoff]
        d += timedelta(days=1)
    recent.sort(key=lambda i: i["ts"], reverse=True)
    write(ANN_FILE, {"updated": stamp(), "items": recent, "pdf": stats})

    events = build_events(s, caps, now)
    write(EVT_FILE, {"updated": stamp(), "items": events})

    print(f"recent: {len(recent)} items (cutoff {cutoff.isoformat()}); events: {len(events)}; "
          f"archive days: {len(days)}; stats: {stats}")


if __name__ == "__main__":
    main()
