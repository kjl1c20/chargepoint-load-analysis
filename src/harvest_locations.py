"""Bronze layer harvester for the CPS network locations feed.

Downloads a dated JSON snapshot of the ChargePlace Scotland locations feed
(charge points with coordinates, connector specs, and live status) to the
Bronze Volume.

Run cadence: weekly — network topology changes slowly.
Idempotent: skips if today's snapshot already exists in the volume.
"""

import os
import json
import logging
from datetime import datetime, timezone

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

try:
    from pyspark.sql import SparkSession
    from pyspark.dbutils import DBUtils
    spark = SparkSession.builder.getOrCreate()
    dbutils = DBUtils(spark)
except ImportError:
    dbutils = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

FEED_URL = os.getenv(
    "LOCATIONS_FEED_URL",
    "https://info.smartcharging.uk/public_feed/locations/2463"
)
VOLUME_PATH = os.getenv(
    "LOCATIONS_VOLUME_PATH",
    "/Volumes/chargepoint_analysis/bronze/locations"
)
FEED_TIMEOUT = int(os.getenv("FEED_TIMEOUT_SECONDS", "60"))

# Alert threshold — warn if the feed returns fewer locations than expected
MIN_EXPECTED_LOCATIONS = int(os.getenv("MIN_EXPECTED_LOCATIONS", "1000"))

logger.info("Configuration: FEED_URL=%s, VOLUME_PATH=%s", FEED_URL, VOLUME_PATH)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((requests.RequestException, requests.Timeout))
)
def _fetch_feed() -> dict:
    logger.debug("Fetching: %s", FEED_URL)
    resp = requests.get(FEED_URL, timeout=FEED_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def harvest() -> dict:
    """Fetch the CPS locations feed and write a dated snapshot to Bronze.

    Returns a result dict with status, filename, and counts.
    """
    if dbutils is None:
        raise RuntimeError("dbutils not available — cannot access Volumes")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    target_name = f"locations_{today}.json"
    target_path = f"{VOLUME_PATH}/{target_name}"
    temp_path = f"{VOLUME_PATH}/.tmp_{target_name}"

    logger.info("CPS LOCATIONS HARVESTER")

    # Idempotency: skip if today's snapshot already exists
    try:
        dbutils.fs.ls(target_path)
        logger.info("Snapshot for %s already exists — skipping", today)
        return {"status": "skipped", "file": target_name}
    except Exception:
        pass

    # Fetch feed with retry
    logger.info("Fetching: %s", FEED_URL)
    raw = _fetch_feed()

    locations = raw.get("data", [])
    total_evses = sum(len(loc.get("evses", [])) for loc in locations)

    logger.info("Feed returned: %d locations, %d EVSEs", len(locations), total_evses)

    if len(locations) < MIN_EXPECTED_LOCATIONS:
        logger.warning(
            "ALERT: Only %d locations returned (expected >= %d) — feed may be incomplete",
            len(locations), MIN_EXPECTED_LOCATIONS
        )

    # Wrap raw data with harvest metadata before writing
    snapshot = {
        "harvested_at": datetime.now(timezone.utc).isoformat(),
        "feed_url": FEED_URL,
        "location_count": len(locations),
        "evse_count": total_evses,
        "status_code": raw.get("status_code"),
        "data": locations,
    }

    payload = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")

    # Write to temp path inside volume, then atomic move — serverless-compatible
    # (same pattern as harvest_cps.py: avoids /Workspace/tmp/ limitations)
    try:
        with open(temp_path, "wb") as f:
            f.write(payload)
        dbutils.fs.mv(temp_path, target_path)
    except Exception as e:
        try:
            dbutils.fs.rm(temp_path)
        except Exception:
            pass
        raise RuntimeError(f"Failed to write snapshot to {target_path}: {e}") from e

    size_mb = len(payload) / 1e6
    logger.info("Written: %s (%.2f MB)", target_name, size_mb)

    # List volume to confirm
    try:
        snapshots = sorted(
            e.name for e in dbutils.fs.ls(VOLUME_PATH)
            if e.name.startswith("locations_") and e.name.endswith(".json")
        )
    except Exception as exc:
        logger.warning("Could not list volume after write: %s", exc)
        snapshots = []

    logger.info("=" * 70)
    logger.info("HARVEST SUMMARY")
    logger.info("=" * 70)
    logger.info("Snapshot : %s", target_name)
    logger.info("Locations: %d | EVSEs: %d", len(locations), total_evses)
    logger.info("Volume   : %d snapshot(s) total", len(snapshots))
    logger.info("=" * 70)

    return {
        "status": "ok",
        "file": target_name,
        "location_count": len(locations),
        "evse_count": total_evses,
        "volume_snapshots": len(snapshots),
    }


if __name__ == "__main__":
    result = harvest()
