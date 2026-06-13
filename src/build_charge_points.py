import os
import json
import logging
from datetime import datetime, timezone

import pandas as pd

try:
    from pyspark.sql import SparkSession
    from pyspark.sql.types import (
        StructType, StructField,
        StringType, DoubleType, IntegerType, TimestampType,
    )
    from pyspark.dbutils import DBUtils
    spark = SparkSession.getActiveSession() or SparkSession.builder.getOrCreate()
    dbutils = DBUtils(spark)
    # One row per physical connector. PK = (cp_id, connector_id).
    SILVER_SCHEMA = StructType([
        StructField("cp_id",               StringType(),    False),
        StructField("connector_id",        StringType(),    False),
        StructField("n_connectors",        IntegerType(),   False),
        StructField("site_name",           StringType(),    True),
        StructField("address",             StringType(),    True),
        StructField("city",                StringType(),    True),
        StructField("postcode",            StringType(),    True),
        StructField("latitude",            DoubleType(),    True),
        StructField("longitude",           DoubleType(),    True),
        StructField("connector_type",      StringType(),    True),
        StructField("max_charge_rate_kw",  DoubleType(),    True),
        StructField("network_status",      StringType(),    True),
        StructField("source_snapshot",     StringType(),    False),
        StructField("ingested_at",         TimestampType(), False),
    ])
except Exception:
    spark = None
    dbutils = None
    SILVER_SCHEMA = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

BRONZE_PATH = os.getenv("LOCATIONS_VOLUME_PATH", "/Volumes/chargepoint_analysis/bronze/locations")
SILVER_TABLE = os.getenv("SILVER_CP_TABLE", "chargepoint_analysis.silver.charge_points")

POWER_TYPE_MAP = {"AC_1_PHASE": "AC", "AC_2_PHASE": "AC", "AC_3_PHASE": "AC", "DC": "DC"}
MIN_EXPECTED_EVSES = int(os.getenv("MIN_EXPECTED_EVSES", "1000"))


def _latest_snapshot() -> str:
    entries = dbutils.fs.ls(BRONZE_PATH)
    snapshots = sorted(
        e.path for e in entries
        if e.name.startswith("Scotland_chargepoint_locations_") and e.name.endswith(".json")
    )
    if not snapshots:
        raise FileNotFoundError(f"No location snapshots found in {BRONZE_PATH}")
    return snapshots[-1].replace("dbfs:", "")


def _flatten(locations: list) -> pd.DataFrame:
    """One row per physical connector (EVSE connectors exploded).

    cp_id = EVSE id; connector_id = the connector's id within the EVSE (joins to
    sessions.connector); n_connectors = how many connectors the EVSE has. Connector
    type/rate are read from the connector itself, not a representative pick.
    """
    rows, skipped = [], 0
    for loc in locations:
        try:
            lat = float(loc["coordinates"]["latitude"])
            lon = float(loc["coordinates"]["longitude"])
        except (KeyError, TypeError, ValueError):
            skipped += 1
            continue
        for evse in loc.get("evses", []):
            cp_id = evse.get("id")
            if cp_id is None:
                skipped += 1
                continue
            connectors = evse.get("connectors", [])
            n_connectors = len(connectors)
            if not connectors:
                # No connector detail → nothing to key a row on (connector_id is PK).
                skipped += 1
                continue
            for conn in connectors:
                connector_id = conn.get("id")
                if connector_id is None:
                    skipped += 1
                    continue
                max_kw = conn.get("max_charge_rate")
                rows.append({
                    "cp_id":              str(cp_id),
                    "connector_id":       str(connector_id),
                    "n_connectors":       n_connectors,
                    "site_name":          loc.get("name"),
                    "address":            loc.get("address"),
                    "city":               loc.get("city"),
                    "postcode":           loc.get("postal_code"),
                    "latitude":           lat,
                    "longitude":          lon,
                    "connector_type":     POWER_TYPE_MAP.get(conn.get("power_type", ""), conn.get("power_type")),
                    "max_charge_rate_kw": float(max_kw) if max_kw is not None else None,
                    "network_status":     evse.get("status"),
                })
    if skipped:
        logger.warning("Skipped %d locations/EVSEs/connectors with missing coords, id, or connector detail", skipped)
    return pd.DataFrame(rows)


def main():
    if spark is None or dbutils is None:
        raise RuntimeError("PySpark/dbutils not available — run in Databricks")

    path = _latest_snapshot()
    logger.info("Reading snapshot: %s", path)

    with open(path) as f:
        snap = json.load(f)

    logger.info("Locations: %d | EVSEs: %d", snap["location_count"], snap["evse_count"])

    df = _flatten(snap["data"])
    if len(df) < MIN_EXPECTED_EVSES:
        raise ValueError(
            f"Only {len(df)} connectors after flatten (expected >= {MIN_EXPECTED_EVSES}). "
            "Aborting write — snapshot may be corrupt or incomplete."
        )
    logger.info("Flattened to %d connector rows across %d charge points",
                len(df), df["cp_id"].nunique())
    df["source_snapshot"] = path.rsplit("/", 1)[-1]
    df["ingested_at"] = pd.Timestamp.utcnow()
    df["n_connectors"] = df["n_connectors"].astype("int32")  # match schema IntegerType

    required = [f.name for f in SILVER_SCHEMA.fields if not f.nullable and f.name in df.columns]
    before = len(df)
    df = df.dropna(subset=required)
    dropped = before - len(df)
    if dropped:
        logger.warning("Dropped %d rows with nulls in non-nullable fields", dropped)

    sdf = spark.createDataFrame(df, schema=SILVER_SCHEMA)
    sdf.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(SILVER_TABLE)

    logger.info("Written %d rows to %s", len(df), SILVER_TABLE)


if __name__ == "__main__":
    main()
