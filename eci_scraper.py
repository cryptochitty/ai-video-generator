"""
ECI Results scraper for TN 2026 Assembly Elections.
Tries sources in order: OpenCity CKAN API → ECI results page → cached data.
On counting day (May 4 2026) update ECI_RESULTS_URL to the actual ECI URL.
"""

import time
import requests
from bs4 import BeautifulSoup

# ── Config ─────────────────────────────────────────────────────────────────────
CACHE_TTL = 600  # 10 minutes

# Update this to the real ECI URL on counting day.
# Typical pattern: https://results.eci.gov.in/ResultsTNLA2026/
ECI_RESULTS_URL = "https://results.eci.gov.in/ResultsTNLA2026/partywiseresult-S22.htm"

# OpenCity CKAN API for TN 2026 candidates (Form 7A)
OPENCITY_URL = (
    "https://data.opencity.in/api/3/action/datastore_search"
    "?resource_id=chennai-list-of-candidates-2026&limit=300"
)

# Maps ECI constituency name variants → app IDs used in App.tsx
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

# Maps ECI party name variants → short party IDs used in App.tsx
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

# ── In-memory cache ─────────────────────────────────────────────────────────────
_cache = {"data": None, "ts": 0}


def _normalize(text: str) -> str:
    return text.strip().lower()


def _map_party(raw: str) -> str:
    norm = _normalize(raw)
    for key, val in PARTY_MAP.items():
        if key in norm:
            return val
    return raw.strip().upper()[:10]


def _map_constituency(raw: str) -> str | None:
    norm = _normalize(raw)
    return CONSTITUENCY_NAME_MAP.get(norm)


def _fetch_eci() -> list[dict]:
    """Scrape the ECI results page for TN."""
    try:
        resp = requests.get(ECI_RESULTS_URL, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        results = []

        # ECI tables typically have class 'table' or 'result-table'
        table = soup.find("table", {"class": lambda c: c and "table" in c})
        if not table:
            table = soup.find("table")
        if not table:
            return []

        rows = table.find_all("tr")[1:]  # skip header
        for row in rows:
            cols = [td.get_text(strip=True) for td in row.find_all("td")]
            if len(cols) < 5:
                continue
            # Typical ECI column order: Constituency | Candidate | Party | Votes | Margin | Status
            constituency_raw = cols[0]
            candidate = cols[1]
            party_raw = cols[2]
            margin_raw = cols[4] if len(cols) > 4 else "N/A"
            status = cols[-1] if cols else "Leading"

            const_id = _map_constituency(constituency_raw)
            if not const_id:
                continue  # not a tracked constituency

            results.append({
                "id": const_id,
                "winner": _map_party(party_raw),
                "winnerCandidate": candidate,
                "actualMargin": margin_raw.replace(",", ""),
                "status": "Won" if "won" in status.lower() else "Leading",
                "predictionCorrect": None,  # frontend resolves this
            })
        return results
    except Exception as e:
        print(f"[ECI scraper] Error: {e}")
        return []


def _fetch_opencity() -> list[dict]:
    """Try OpenCity CKAN API for candidate/result data."""
    try:
        resp = requests.get(OPENCITY_URL, timeout=10)
        resp.raise_for_status()
        payload = resp.json()
        if not payload.get("success"):
            return []
        records = payload.get("result", {}).get("records", [])
        results = []
        for r in records:
            const_id = _map_constituency(r.get("constituency_name", ""))
            if not const_id:
                continue
            # Only include if result is declared
            if not r.get("winner_candidate"):
                continue
            results.append({
                "id": const_id,
                "winner": _map_party(r.get("party_name", "")),
                "winnerCandidate": r.get("winner_candidate", ""),
                "actualMargin": str(r.get("margin", "N/A")),
                "status": r.get("result_status", "Declared"),
                "predictionCorrect": None,
            })
        return results
    except Exception as e:
        print(f"[OpenCity] Error: {e}")
        return []


def get_tn_results() -> list[dict]:
    """
    Returns fresh TN constituency results, using cache if < 10 min old.
    Tries OpenCity first, then ECI scraper.
    """
    now = time.time()
    if _cache["data"] is not None and (now - _cache["ts"]) < CACHE_TTL:
        return _cache["data"]

    data = _fetch_opencity()
    if not data:
        data = _fetch_eci()

    if data:
        _cache["data"] = data
        _cache["ts"] = now
    return _cache["data"] or []
