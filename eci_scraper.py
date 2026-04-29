"""
ECI Results scraper for TN 2026 Assembly Elections.

Design principles:
- Background thread fetches results every 10 minutes. API endpoints serve from cache only.
- Zero per-request scraping — the cache is always ready so ECI servers are never hit
  by individual user requests (no throttling risk).
- Source priority: OpenCity CKAN → ECI results page → NDTV/The Hindu fallbacks → last known good.
- Last-known-good data persists across failures so the API never returns empty on counting day.

On counting day (May 4 2026): update ECI_RESULTS_URL below to the actual ECI URL.
"""

import time
import threading
import requests
from bs4 import BeautifulSoup

# ── Config ─────────────────────────────────────────────────────────────────────
FETCH_INTERVAL = 600          # scrape every 10 minutes
REQUEST_TIMEOUT = 15
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
}

# ── Source URLs (update ECI_RESULTS_URL on counting day) ───────────────────────
# Pattern from past elections: https://results.eci.gov.in/ResultsAdv2021/
# For 2026 TN it will likely be: https://results.eci.gov.in/ResultsTNLA2026/
ECI_RESULTS_URL = "https://results.eci.gov.in/ResultsTNLA2026/partywiseresult-S22.htm"

# OpenCity CKAN — structured JSON, updated by volunteers on counting day
OPENCITY_URL = (
    "https://data.opencity.in/api/3/action/datastore_search"
    "?resource_id=tn-assembly-2026-results&limit=300"
)

# Alternate: NDTV election results API (used during 2021, likely same pattern)
NDTV_URL = "https://results.ndtv.com/results/assembly/tamil-nadu-2026/data.json"

# Alternate: The Hindu results API
HINDU_URL = "https://www.thehindu.com/elections/results/tamil-nadu-2026/data.json"

# ── Name maps ──────────────────────────────────────────────────────────────────
CONSTITUENCY_NAME_MAP = {
    "r k nagar": "rk-nagar",
    "rk nagar": "rk-nagar",
    "kolathur": "kolathur",
    "bodinayakanur": "bodinayakkanur",
    "bodinayakkanur": "bodinayakkanur",
    "coimbatore south": "coimbatore-south",
    "edapadi": "edappadi",
    "edappadi": "edappadi",
    "dindigul": "dindigul",
    "madurai central": "madurai-central",
    "tiruchirappalli east": "trichy-east",
    "trichy east": "trichy-east",
    "trichy (east)": "trichy-east",
    "villupuram": "villupuram",
    "thanjavur": "thanjavur",
    "coimbatore north": "coimbatore-north",
    "salem south": "salem-south",
    "erode east": "erode-east",
    "tiruppur south": "tiruppur-south",
    "thoothukudi": "thoothukudi",
    "ramanathapuram": "ramanathapuram",
    "kancheepuram": "kancheepuram",
}

PARTY_MAP = {
    "dravida munnetra kazhagam": "DMK",
    "dmk": "DMK",
    "all india anna dravida munnetra kazhagam": "AIADMK",
    "aiadmk": "AIADMK",
    "tamilaga vettri kazhagam": "TVK",
    "tvk": "TVK",
    "naam tamilar katchi": "NTK",
    "ntk": "NTK",
    "bharatiya janata party": "BJP",
    "bjp": "BJP",
    "indian national congress": "INC",
    "inc": "INC",
    "independent": "IND",
}

# ── Cache ───────────────────────────────────────────────────────────────────────
# last_good survives failed fetches — never returns empty on counting day
_cache = {
    "current": [],     # latest fetch result (may be empty on failure)
    "last_good": [],   # last successful non-empty fetch
    "fetched_at": 0,   # epoch of last fetch attempt
    "source": None,    # which source succeeded last
}
_lock = threading.Lock()


# ── Helpers ─────────────────────────────────────────────────────────────────────
def _norm(text: str) -> str:
    return text.strip().lower()


def _map_party(raw: str) -> str:
    norm = _norm(raw)
    for key, val in PARTY_MAP.items():
        if key in norm:
            return val
    return raw.strip().upper()[:12]


def _map_const(raw: str) -> str | None:
    return CONSTITUENCY_NAME_MAP.get(_norm(raw))


def _make_result(const_id, winner_party, candidate, margin, status="Declared"):
    return {
        "id": const_id,
        "winner": _map_party(winner_party),
        "winnerCandidate": candidate.strip(),
        "actualMargin": str(margin).replace(",", "").strip(),
        "status": status,
        "predictionCorrect": None,   # frontend resolves: winner === c.leading
    }


# ── Source fetchers ─────────────────────────────────────────────────────────────
def _fetch_opencity() -> list[dict]:
    try:
        r = requests.get(OPENCITY_URL, timeout=REQUEST_TIMEOUT, headers=HEADERS)
        r.raise_for_status()
        payload = r.json()
        if not payload.get("success"):
            return []
        out = []
        for rec in payload.get("result", {}).get("records", []):
            cid = _map_const(rec.get("constituency_name", ""))
            if not cid or not rec.get("winner_candidate"):
                continue
            out.append(_make_result(
                cid,
                rec.get("party_name", ""),
                rec.get("winner_candidate", ""),
                rec.get("margin", "N/A"),
                rec.get("result_status", "Declared"),
            ))
        return out
    except Exception as e:
        print(f"[OpenCity] {e}")
        return []


def _parse_eci_table(soup) -> list[dict]:
    """Parse the standard ECI results HTML table."""
    out = []
    table = (
        soup.find("table", {"class": lambda c: c and "result" in c.lower()})
        or soup.find("table", {"id": lambda i: i and "result" in i.lower()})
        or soup.find("table")
    )
    if not table:
        return []
    for row in table.find_all("tr")[1:]:
        cols = [td.get_text(" ", strip=True) for td in row.find_all("td")]
        if len(cols) < 5:
            continue
        # ECI column order: Constituency | Candidate | Party | Total Votes | Margin | Status
        cid = _map_const(cols[0])
        if not cid:
            continue
        out.append(_make_result(
            cid, cols[2], cols[1], cols[4] if len(cols) > 4 else "N/A",
            cols[-1] if len(cols) > 5 else "Leading",
        ))
    return out


def _fetch_eci() -> list[dict]:
    try:
        r = requests.get(ECI_RESULTS_URL, timeout=REQUEST_TIMEOUT, headers=HEADERS)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        return _parse_eci_table(soup)
    except Exception as e:
        print(f"[ECI] {e}")
        return []


def _fetch_ndtv() -> list[dict]:
    try:
        r = requests.get(NDTV_URL, timeout=REQUEST_TIMEOUT, headers=HEADERS)
        r.raise_for_status()
        data = r.json()
        out = []
        for item in data.get("constituencies", data.get("results", [])):
            cid = _map_const(item.get("name", item.get("constituency", "")))
            if not cid:
                continue
            out.append(_make_result(
                cid,
                item.get("leading_party", item.get("party", "")),
                item.get("leading_candidate", item.get("candidate", "")),
                item.get("margin", "N/A"),
                "Won" if item.get("result_type", "").lower() == "won" else "Leading",
            ))
        return out
    except Exception as e:
        print(f"[NDTV] {e}")
        return []


def _fetch_hindu() -> list[dict]:
    try:
        r = requests.get(HINDU_URL, timeout=REQUEST_TIMEOUT, headers=HEADERS)
        r.raise_for_status()
        data = r.json()
        out = []
        for item in data.get("data", []):
            cid = _map_const(item.get("constituency_name", ""))
            if not cid:
                continue
            out.append(_make_result(
                cid,
                item.get("party_name", ""),
                item.get("candidate_name", ""),
                item.get("vote_margin", "N/A"),
            ))
        return out
    except Exception as e:
        print(f"[TheHindu] {e}")
        return []


# ── Background worker ────────────────────────────────────────────────────────────
def _do_fetch():
    """Try sources in priority order. Update cache. Schedule next run."""
    sources = [
        ("OpenCity", _fetch_opencity),
        ("ECI",      _fetch_eci),
        ("NDTV",     _fetch_ndtv),
        ("TheHindu", _fetch_hindu),
    ]
    results, source_name = [], None
    for name, fn in sources:
        data = fn()
        if data:
            results, source_name = data, name
            print(f"[ECI scraper] {len(data)} results from {name}")
            break
        else:
            print(f"[ECI scraper] {name} returned no data, trying next source")

    with _lock:
        _cache["current"] = results
        _cache["fetched_at"] = time.time()
        if results:
            _cache["last_good"] = results
            _cache["source"] = source_name

    # Schedule next fetch
    t = threading.Timer(FETCH_INTERVAL, _do_fetch)
    t.daemon = True
    t.start()


def start_background_fetcher():
    """Call once at app startup. First fetch happens immediately in a thread."""
    t = threading.Thread(target=_do_fetch, daemon=True)
    t.start()


# ── Public API ───────────────────────────────────────────────────────────────────
def get_tn_results() -> tuple[list[dict], dict]:
    """
    Returns (results, meta). Always returns last_good data if current fetch failed.
    Never blocks — serves from in-memory cache only.
    """
    with _lock:
        results = _cache["current"] or _cache["last_good"]
        meta = {
            "source": _cache["source"],
            "fetched_at": _cache["fetched_at"],
            "using_last_good": not _cache["current"] and bool(_cache["last_good"]),
            "count": len(results),
        }
    return results, meta
