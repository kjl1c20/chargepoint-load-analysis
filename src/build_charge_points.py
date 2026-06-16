import os
import json
import logging
from datetime import datetime, timezone

import pandas as pd

try:
    from pyspark.sql import SparkSession, functions as F
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
        StructField("postcode_source",     StringType(),    True),
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
    F = None
    SILVER_SCHEMA = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

BRONZE_PATH = os.getenv("LOCATIONS_VOLUME_PATH", "/Volumes/chargepoint_analysis/bronze/locations")
SILVER_TABLE = os.getenv("SILVER_CP_TABLE", "chargepoint_analysis.silver.charge_points")
# Curated, human-approved postcode corrections: cp_id → correct postcode. Applied fix-on-read
# (Bronze stays immutable). Hand-maintained — the git commit is the audit trail (who/when/why).
POSTCODE_OVERRIDES = {
    "61203": "ML11 8RP",  # NHS State Hospital Visitors Car Park
    "61204": "ML11 8RP",  # NHS State Hospital Visitors Car Park
    "61205": "ML11 8RP",  # NHS State Hospital Visitors Car Park
    "61206": "ML11 8RP",  # NHS State Hospital Visitors Car Park
    "61691": "FK10 4LD"   # Devonway
}

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
    """One row per physical connector.

    The feed lists each connector as its own EVSE entry, and entries that share the same
    `id` are connectors of the same charge point (e.g. id "52118" appears twice, uid
    "52118_1"/"52118_2", connector ids 1/2). So:
      cp_id        = evse id (shared across an EVSE's connectors)
      connector_id = the connector's id within the charge point (joins to sessions.connector)
      n_connectors = how many distinct connectors share this cp_id (counted in a second pass)
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

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    # A connector can appear more than once across entries — keep one row per (cp_id, connector_id).
    df = df.drop_duplicates(subset=["cp_id", "connector_id"], keep="first").reset_index(drop=True)
    # n_connectors = distinct connectors sharing this cp_id (the real per-charge-point count).
    df["n_connectors"] = df.groupby("cp_id")["connector_id"].transform("nunique").astype("int32")
    return df


def _apply_overrides(sdf):
    """Fix-on-read: replace the feed postcode with a curated override where one exists.

    Deterministic, in-code mapping — Bronze is never mutated. Empty mapping = no-op
    (postcode_source already 'feed').
    """
    if not POSTCODE_OVERRIDES:
        return sdf

    corrected = F.create_map([F.lit(x) for kv in POSTCODE_OVERRIDES.items() for x in kv])[F.col("cp_id")]
    out = (
        sdf.withColumn("postcode", F.coalesce(corrected, F.col("postcode")))
        .withColumn("postcode_source",
                    F.when(corrected.isNotNull(), F.lit("override")).otherwise(F.lit("feed")))
    )
    logger.info("Applied %d curated postcode override(s)", len(POSTCODE_OVERRIDES))
    return out


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
    df["postcode_source"] = "feed"  # may flip to 'override' below

    required = [f.name for f in SILVER_SCHEMA.fields if not f.nullable and f.name in df.columns]
    before = len(df)
    df = df.dropna(subset=required)
    dropped = before - len(df)
    if dropped:
        logger.warning("Dropped %d rows with nulls in non-nullable fields", dropped)

    # createDataFrame maps pandas → Spark by position, so align to the schema field order
    # (n_connectors is appended last by the groupby above).
    df = df[[f.name for f in SILVER_SCHEMA.fields]]
    sdf = spark.createDataFrame(df, schema=SILVER_SCHEMA)
    sdf = _apply_overrides(sdf)
    sdf.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(SILVER_TABLE)

    logger.info("Written %d rows to %s", len(df), SILVER_TABLE)


if __name__ == "__main__":
    main()
