"""Fetch CPS locations feed snapshot to Bronze Volume. Run locally — feed blocks cloud IPs."""

import io
import os
import json
import logging
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import ResourceDoesNotExist, NotFound

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

FEED_URL = os.getenv("LOCATIONS_FEED_URL", "https://info.smartcharging.uk/public_feed/locations/2463")
VOLUME_PATH = os.getenv("LOCATIONS_VOLUME_PATH", "/Volumes/chargepoint_analysis/bronze/locations")
FEED_TIMEOUT = int(os.getenv("FEED_TIMEOUT_SECONDS", "60"))
MIN_EXPECTED_LOCATIONS = int(os.getenv("MIN_EXPECTED_LOCATIONS", "1000"))

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ChargePoint-Analysis/1.0)",
    "Accept": "application/json",
}


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((requests.RequestException, requests.Timeout)),
)
def _fetch_feed() -> dict:
    resp = requests.get(FEED_URL, headers=_HEADERS, timeout=FEED_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def harvest() -> dict:
    w = WorkspaceClient(
        host=os.getenv("DATABRICKS_HOST"),
        token=os.getenv("DATABRICKS_TOKEN"),
    )
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    target_path = f"{VOLUME_PATH}/locations_{today}.json"

    try:
        w.files.get_metadata(target_path)
        logger.info("Snapshot for %s already exists — skipping", today)
        return {"status": "skipped"}
    except (ResourceDoesNotExist, NotFound):
        pass

    logger.info("Fetching %s", FEED_URL)
    raw = _fetch_feed()
    locations = raw.get("data", [])
    total_evses = sum(len(loc.get("evses", [])) for loc in locations)

    if len(locations) < MIN_EXPECTED_LOCATIONS:
        logger.warning("Only %d locations returned — feed may be incomplete", len(locations))

    snapshot = {
        "harvested_at": datetime.now(timezone.utc).isoformat(),
        "feed_url": FEED_URL,
        "location_count": len(locations),
        "evse_count": total_evses,
        "status_code": raw.get("status_code"),
        "data": locations,
    }

    payload = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
    w.files.upload(target_path, io.BytesIO(payload), overwrite=False)

    logger.info("Uploaded %s — %d locations, %d EVSEs (%.2f MB)",
                target_path.rsplit("/", 1)[-1], len(locations), total_evses, len(payload) / 1e6)

    return {"status": "ok", "file": target_path, "location_count": len(locations), "evse_count": total_evses}


if __name__ == "__main__":
    harvest()
