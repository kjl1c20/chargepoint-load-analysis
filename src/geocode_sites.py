"""Geocode CPS site names to lat/lon + local authority via Nominatim, with disk cache."""

import json
import time
import logging
from pathlib import Path

import requests
import pandas as pd

from utils import get_latest_snapshot_id  # noqa: F401  (kept for parity; not required)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
HEADERS = {"User-Agent": "chargepoint-load-analysis/1.0 (research; contact via repo)"}
CACHE_PATH = Path("./data/reference/site_geocode_cache.json")
CLEAN_PATH = Path("./data/clean/cps_sessions_clean.parquet")

# rough Scotland bounding box, to reject geocodes that land elsewhere in the UK
LAT_MIN, LAT_MAX = 54.5, 61.0
LON_MIN, LON_MAX = -9.0, -0.5
THROTTLE_SECONDS = 1.1  # Nominatim usage policy: <= 1 request/second


def _query(q: str) -> dict | None:
    """One Nominatim lookup; returns location dict or None (incl. out-of-Scotland)."""
    resp = requests.get(
        NOMINATIM_URL,
        params={"q": f"{q}, Scotland, UK", "format": "json",
                "addressdetails": 1, "countrycodes": "gb", "limit": 1},
        headers=HEADERS, timeout=20
    )
    resp.raise_for_status()
    data = resp.json()
    if not data:
        return None

    top = data[0]
    lat, lon = float(top["lat"]), float(top["lon"])
    if not (LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX):
        return None

    addr = top.get("address", {})
    la = addr.get("council") or addr.get("state_district") or addr.get("county") or addr.get("city")
    return {
        "latitude": lat,
        "longitude": lon,
        "local_authority": la,
        "postcode": addr.get("postcode"),
        "address": top.get("display_name")
    }


def geocode_site(name: str) -> dict:
    """Resolve one site name via a fallback chain. Always returns a dict."""
    parts = [p.strip() for p in str(name).split(",") if p.strip()]
    queries = [name]
    if len(parts) > 1:
        queries += [parts[0], parts[-1]]  # first chunk, then trailing town

    for i, q in enumerate(queries):
        try:
            res = _query(q)
        except requests.RequestException as e:
            logger.warning("geocode error for %r: %s", q, e)
            res = None
        time.sleep(THROTTLE_SECONDS)
        if res:
            res["geocode_method"] = "full" if i == 0 else ("first_chunk" if q == parts[0] else "town")
            res["geocode_query"] = q
            return res

    return {"geocode_method": "miss"}


def geocode_all(names, cache_path: Path = CACHE_PATH) -> dict:
    """Geocode all `names`, skipping any already cached; saves cache incrementally."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}

    todo = [n for n in names if n and n not in cache]
    logger.info("Geocoding | %d to do | %d already cached", len(todo), len(cache))

    for i, name in enumerate(todo, 1):
        cache[name] = geocode_site(name)
        if i % 25 == 0:
            cache_path.write_text(json.dumps(cache))
            logger.info("  progress %d/%d", i, len(todo))

    cache_path.write_text(json.dumps(cache))
    return cache


if __name__ == "__main__":
    sites = pd.read_parquet(CLEAN_PATH, columns=["site_name"])["site_name"].dropna().unique().tolist()
    cache = geocode_all(sites)
    resolved = sum(1 for v in cache.values() if v.get("geocode_method") != "miss")
    logger.info("Geocode complete | %d/%d resolved", resolved, len(cache))
