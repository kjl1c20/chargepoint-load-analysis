"""
Harvest ChargePlace Scotland public charging-session data.

CPS publishes monthly "Sessions" spreadsheets on its Monthly Charge Point
Performance page. This scrapes the page for those .xlsx links, downloads them,
normalises the (inconsistently named) columns to one schema, and writes a
combined parquet.

Notes / limitations:
  - The session files contain NO geography (no postcode/lat-long). A separate
    cp_id -> location join is needed for postcode-district analysis.
  - The data month is derived from the `start_time` column itself, not the
    (wildly inconsistent) filenames.
  - CPS is mid-transition through 2025-2026; the network is fragmenting, so
    later months cover a shrinking set of chargers. Control for this with a
    like-for-like cp_id cohort in any trend analysis.

Run:  poetry run python src/harvest_cps.py
Needs: openpyxl  (poetry add openpyxl)
"""

import re
import logging
from pathlib import Path

import requests
import pandas as pd


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)

CPS_PERFORMANCE_URL = "https://chargeplacescotland.org/monthly-charge-point-performance/"
CPS_MEDIA_API = "https://chargeplacescotland.org/wp-json/wp/v2/media"
RAW_CPS_DIR = Path("./data/raw_cps")

# normalise the many header spellings across eras to one schema
COLUMN_MAP = {
    # site
    "site": "site_name",
    "sites": "site_name",
    "site_name": "site_name",
    # charge point id
    "cp id": "cp_id",
    "cp_id": "cp_id",
    "cpid": "cp_id",
    "charging_point_id": "cp_id",
    "display id": "cp_id",
    # connector type (rapid/ac)
    "connector type": "connector_type",
    "connector_type": "connector_type",
    "charging_type": "connector_type",
    "te_charge_type": "connector_type",
    # connector number/id
    "connector": "connector",
    "connector id": "connector",
    "connector_id": "connector",
    "connecto id": "connector",
    # currency
    "currency": "currency",
    "curr": "currency",
    # amount paid
    "amount": "amount",
    "amt": "amount",
    # energy consumed
    "consum": "consumption_kwh",
    "consum(kwh)": "consumption_kwh",
    "consumed": "consumption_kwh",
    # duration / times
    "duration": "duration",
    "start": "start_time",
    "start time": "start_time",
    "starttime": "start_time",
    "start_time": "start_time",
    "end": "end_time",
}


def _links_from_media_api() -> list[str]:
    """
    Discover session-file URLs via the WordPress REST media API — structured
    JSON with pagination, more robust than scraping the HTML page.
    """
    links = []
    page = 1
    while True:
        resp = requests.get(
            CPS_MEDIA_API,
            params={"search": "sessions", "per_page": 100, "page": page,
                    "_fields": "source_url"},
            timeout=60
        )
        # WP returns 400 once you page past the last page
        if resp.status_code == 400:
            break
        resp.raise_for_status()
        items = resp.json()
        if not items:
            break
        for item in items:
            url = item.get("source_url", "")
            if re.search(r"SESSIONS[^/]*\.xlsx$", url, flags=re.IGNORECASE):
                links.append(url)
        if page >= int(resp.headers.get("X-WP-TotalPages", page)):
            break
        page += 1
    return sorted(set(links))


def _links_from_html() -> list[str]:
    """Fallback: scrape the performance page for 'Sessions' .xlsx links."""
    resp = requests.get(CPS_PERFORMANCE_URL, timeout=60)
    resp.raise_for_status()
    links = re.findall(r'href="([^"]+SESSIONS[^"]*\.xlsx)"', resp.text, flags=re.IGNORECASE)
    return sorted(set(links))


def fetch_session_file_links() -> list[str]:
    """
    All CPS 'Sessions' .xlsx URLs. Unions the WP media API (primary, more
    complete — also lists older files no longer on the page) with the HTML
    scrape (catches the occasional file the API search misses). Either source
    failing is tolerated, so discovery degrades gracefully.
    """
    links = set()
    for name, source in (("media API", _links_from_media_api), ("HTML scrape", _links_from_html)):
        try:
            found = source()
            logger.info("Found %d session files via %s", len(found), name)
            links.update(found)
        except requests.RequestException as e:
            logger.warning("%s failed: %s", name, e)

    if not links:
        raise RuntimeError("Could not discover any session files (both sources failed).")

    logger.info("Total unique session files: %d", len(links))
    return sorted(links)


def _download(url: str) -> Path:
    """Download a file to RAW_CPS_DIR (skip if already present)."""
    RAW_CPS_DIR.mkdir(parents=True, exist_ok=True)
    dest = RAW_CPS_DIR / url.rsplit("/", 1)[-1]
    if dest.exists():
        return dest
    resp = requests.get(url, timeout=180)
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    logger.info("Downloaded %s (%.1f MB)", dest.name, len(resp.content) / 1e6)
    return dest


REQUIRED_COLS = ["cp_id", "connector", "start_time", "consumption_kwh", "duration"]
OPTIONAL_COLS = ["site_name", "connector_type", "currency", "amount"]
OUTPUT_COLS = [
    "site_name", "cp_id", "connector_type", "connector", "currency",
    "amount", "consumption_kwh", "duration_minutes", "start_time",
    "end_time", "source_file"
]


def _normalise(df: pd.DataFrame, source_file: str) -> pd.DataFrame:
    """Map any era's headers to one schema; derive end_time/duration_minutes."""
    df = df.rename(columns={c: COLUMN_MAP.get(c.strip().lower(), c.strip().lower()) for c in df.columns})

    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise KeyError(f"missing required columns {missing} (have {list(df.columns)})")

    for col in OPTIONAL_COLS:
        if col not in df.columns:
            df[col] = pd.NA

    # cp_id / connector are integer in some files and string codes in others —
    # force string so the combined columns have one type (parquet needs this)
    df["cp_id"] = df["cp_id"].astype(str).str.strip()
    df["connector"] = df["connector"].astype(str).str.strip()

    # CPS dates are UK day-first (DD/MM/YYYY); dayfirst=True prevents silent
    # day/month swaps on the CSV exports (xlsx datetimes are unaffected)
    df["start_time"] = pd.to_datetime(df["start_time"], errors="coerce", dayfirst=True)
    df["consumption_kwh"] = pd.to_numeric(df["consumption_kwh"], errors="coerce")
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")

    # duration comes in two formats across files: HH:MM:SS strings/time/Timedelta,
    # OR a bare number of seconds (e.g. the Sept-24 file). Handle both.
    if pd.api.types.is_numeric_dtype(df["duration"]):
        dur = pd.to_timedelta(pd.to_numeric(df["duration"], errors="coerce"), unit="s")
    else:
        dur = pd.to_timedelta(df["duration"].astype(str), errors="coerce")
    df["duration_minutes"] = dur.dt.total_seconds() / 60
    # prefer a real end column where present, else derive from start + duration
    if "end_time" in df.columns:
        df["end_time"] = pd.to_datetime(df["end_time"], errors="coerce", dayfirst=True)
    else:
        df["end_time"] = df["start_time"] + dur

    df["source_file"] = source_file
    return df[OUTPUT_COLS]  # fixed schema; drops stray columns (e.g. SDR_ID)


def harvest_cps_sessions(limit: int | None = None) -> pd.DataFrame:
    """Download + normalise + combine all CPS session files into one DataFrame."""
    links = fetch_session_file_links()
    if limit:
        links = links[-limit:]

    frames = []
    for url in links:
        try:
            path = _download(url)
            raw = pd.read_excel(path)
            frames.append(_normalise(raw, path.name))
        except Exception as e:  # keep going if one file is malformed
            logger.warning("Skipped %s: %s", url, e)

    df = pd.concat(frames, ignore_index=True)
    before = len(df)
    df = df.drop_duplicates(subset=["cp_id", "connector", "start_time", "consumption_kwh"])
    logger.info("Combined %s rows (%s after dedupe)", f"{before:,}", f"{len(df):,}")

    df = df.dropna(subset=["start_time"])
    df["month"] = df["start_time"].dt.to_period("M")
    return df


# ============================================================
# run
# ============================================================

if __name__ == "__main__":
    sessions = harvest_cps_sessions()

    logger.info(
        "Harvest complete | %s sessions | %s charge points | months %s -> %s",
        f"{len(sessions):,}",
        f"{sessions['cp_id'].nunique():,}",
        sessions["month"].min(),
        sessions["month"].max()
    )
    logger.info("Sessions per month:\n%s", sessions.groupby("month").size())

    out = RAW_CPS_DIR / "cps_sessions_all.parquet"
    sessions.to_parquet(out, index=False)
    logger.info("Saved | %s", out)
