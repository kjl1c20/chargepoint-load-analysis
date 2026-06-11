"""Silver layer cleaner: reads raw CPS files from Bronze Volume, cleans and writes to Silver Delta table.

Run modes:
- Incremental (default): processes only Bronze files not yet present in Silver
- Full refresh: reprocesses all Bronze files (set FULL_REFRESH=true env var or Databricks widget)
"""

import os
import json
import logging

import pandas as pd

try:
    from pyspark.sql import SparkSession
    from pyspark.sql.types import (
        StructType, StructField,
        StringType, DoubleType, TimestampType
    )
    from pyspark.dbutils import DBUtils
    spark = SparkSession.builder.getOrCreate()
    dbutils = DBUtils(spark)
    SILVER_SCHEMA = StructType([
        StructField("site_name",        StringType(),    True),
        StructField("cp_id",            StringType(),    False),
        StructField("connector_type",   StringType(),    True),
        StructField("connector",        StringType(),    False),
        StructField("currency",         StringType(),    True),
        StructField("amount",           DoubleType(),    True),
        StructField("consumption_kwh",  DoubleType(),    False),
        StructField("duration_minutes", DoubleType(),    False),
        StructField("start_time",       TimestampType(), False),
        StructField("end_time",         TimestampType(), True),
        StructField("source_file",      StringType(),    False),
        StructField("ingested_at",      TimestampType(), False),
        StructField("year_month",       StringType(),    False),
    ])
except Exception:
    spark = None
    dbutils = None
    SILVER_SCHEMA = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger(__name__)

# Configuration
BRONZE_VOLUME_PATH = os.getenv("BRONZE_VOLUME_PATH", "/Volumes/chargepoint_analysis/bronze/raw_cps")
SILVER_TABLE = os.getenv("SILVER_TABLE", "chargepoint_analysis.silver.cps_sessions_clean")
FULL_REFRESH = os.getenv("FULL_REFRESH", "false").lower() == "true"

# Maps all known header spellings across monthly files to one schema
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
    # connector type
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
    "duration_time": "duration",
    "start": "start_time",
    "start time": "start_time",
    "starttime": "start_time",
    "start_time": "start_time",
    "end": "end_time",
}

REQUIRED_COLS = ["cp_id", "connector", "start_time", "consumption_kwh", "duration"]
OPTIONAL_COLS = ["site_name", "connector_type", "currency", "amount"]
OUTPUT_COLS = [
    "site_name", "cp_id", "connector_type", "connector", "currency",
    "amount", "consumption_kwh", "duration_minutes", "start_time",
    "end_time", "source_file", "ingested_at", "year_month",
]

MAX_DURATION_MINUTES = 24 * 60
MAX_CONSUMPTION_KWH = 300

# Known CPS connector type values — explicit map avoids title-casing mangling abbreviations
CONNECTOR_TYPE_MAP = {
    "ac": "AC",
    "dc": "DC",
    "rapid": "Rapid",
    "rapid dc": "Rapid DC",
    "fast ac": "Fast AC",
    "slow ac": "Slow AC",
}

# cp_id / connector values that indicate a missing identifier after string coercion
INVALID_ID_VALUES = {"nan", "none", "na", ""}


def get_processed_files() -> set:
    """Return source_file values already written to the Silver table."""
    if spark is None or not spark.catalog.tableExists(SILVER_TABLE):
        return set()
    try:
        df = spark.sql(f"SELECT DISTINCT source_file FROM {SILVER_TABLE}")
        return {row.source_file for row in df.collect()}
    except Exception as e:
        raise RuntimeError(f"Failed to query Silver table {SILVER_TABLE}: {e}") from e


def list_bronze_files() -> list:
    """List all xlsx/csv files in the Bronze Volume."""
    try:
        entries = dbutils.fs.ls(BRONZE_VOLUME_PATH)
    except Exception as e:
        raise RuntimeError(f"Cannot access Bronze Volume at {BRONZE_VOLUME_PATH}: {e}") from e
    return [
        e.path for e in entries
        if e.name.lower().endswith((".xlsx", ".csv")) and not e.isDir()
    ]


def _read_file(path: str) -> pd.DataFrame:
    """Read xlsx or csv from a Volume path into a DataFrame."""
    local_path = path.replace("dbfs:", "")
    if local_path.lower().endswith(".csv"):
        for enc in ("utf-8", "latin-1"):
            try:
                return pd.read_csv(local_path, encoding=enc)
            except UnicodeDecodeError:
                continue
        return pd.read_csv(local_path, encoding="latin-1", engine="python")
    return pd.read_excel(local_path)


def _fix_blank_duration(df: pd.DataFrame) -> pd.DataFrame:
    """Some files export the duration column with a blank 'Unnamed: N' header."""
    lower = [str(c).strip().lower() for c in df.columns]
    if "duration" not in lower:
        unnamed = [c for c in df.columns if str(c).strip().lower().startswith("unnamed")]
        if len(unnamed) == 1:
            df = df.rename(columns={unnamed[0]: "duration"})
    return df


def _normalise(df: pd.DataFrame, source_file: str) -> pd.DataFrame:
    """Map any era's column headers to one schema and derive typed columns.

    Handles all known header spelling variations, UK day-first dates, and the
    Sept-2024 duration format (bare seconds instead of HH:MM:SS).
    """
    df = df.rename(columns={
        c: COLUMN_MAP.get(c.strip().lower(), c.strip().lower()) for c in df.columns
    })

    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise KeyError(f"Missing required columns {missing} — have {list(df.columns)}")

    for col in OPTIONAL_COLS:
        if col not in df.columns:
            df[col] = pd.NA

    df["cp_id"] = df["cp_id"].astype(str).str.strip()
    df["connector"] = df["connector"].astype(str).str.strip()

    # CPS dates are UK day-first (DD/MM/YYYY); dayfirst prevents silent day/month swaps
    df["start_time"] = pd.to_datetime(df["start_time"], errors="coerce", dayfirst=True)
    df["consumption_kwh"] = pd.to_numeric(df["consumption_kwh"], errors="coerce")
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")

    # Duration is HH:MM:SS in most files, but bare seconds in the Sept-2024 file
    if pd.api.types.is_numeric_dtype(df["duration"]):
        dur = pd.to_timedelta(pd.to_numeric(df["duration"], errors="coerce"), unit="s")
    else:
        dur = pd.to_timedelta(df["duration"].astype(str), errors="coerce")
    df["duration_minutes"] = dur.dt.total_seconds() / 60

    if "end_time" in df.columns:
        df["end_time"] = pd.to_datetime(df["end_time"], errors="coerce", dayfirst=True)
    else:
        df["end_time"] = df["start_time"] + dur

    df["source_file"] = source_file
    return df


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    """Deduplicate, filter invalid sessions, and normalise strings."""
    before = len(df)
    df = df.drop_duplicates(subset=["cp_id", "connector", "start_time", "consumption_kwh"])
    logger.info("Dedup: dropped %d rows", before - len(df))

    # Drop rows where cp_id or connector is a null sentinel string produced by astype(str)
    before = len(df)
    df = df[
        ~df["cp_id"].str.lower().isin(INVALID_ID_VALUES)
        & ~df["connector"].str.lower().isin(INVALID_ID_VALUES)
    ]
    logger.info("Null identifier filter: dropped %d rows", before - len(df))

    df["cp_id"] = df["cp_id"].str.strip()

    # Preserve nulls — only apply string ops to non-null rows
    mask = df["site_name"].notna()
    df.loc[mask, "site_name"] = df.loc[mask, "site_name"].astype(str).str.strip().str.title()
    df.loc[~mask, "site_name"] = None

    # Standardise connector_type — vectorised: map known values, title-case unknowns, preserve nulls
    ct_mask = df["connector_type"].notna()
    ct_clean = df.loc[ct_mask, "connector_type"].astype(str).str.strip()
    mapped = ct_clean.str.lower().map(CONNECTOR_TYPE_MAP)
    df.loc[ct_mask, "connector_type"] = mapped.where(mapped.notna(), ct_clean.str.title())
    df.loc[~ct_mask, "connector_type"] = None

    before = len(df)
    df = df[
        (df["consumption_kwh"] > 0)
        & (df["consumption_kwh"] <= MAX_CONSUMPTION_KWH)
        & (df["duration_minutes"] > 1)
        & (df["duration_minutes"] <= MAX_DURATION_MINUTES)
        & (df["end_time"] > df["start_time"])
    ]
    logger.info("Invalid session filter: dropped %d rows", before - len(df))
    return df


def process_files(file_paths: list) -> tuple:
    """Read, normalise, and clean a list of Bronze files. Returns (DataFrame, loaded, skipped)."""
    frames, loaded, skipped = [], [], []

    for path in sorted(file_paths):
        name = path.rsplit("/", 1)[-1]
        try:
            raw = _fix_blank_duration(_read_file(path))
            frames.append(_normalise(raw, name))
            loaded.append(name)
            logger.info("Loaded: %s (%d rows)", name, len(frames[-1]))
        except Exception as e:
            skipped.append({"file": name, "reason": str(e)})
            logger.warning("Skipped %s: %s", name, e)

    if not frames:
        return pd.DataFrame(), loaded, skipped

    df = pd.concat(frames, ignore_index=True)
    df = _clean(df)
    df["ingested_at"] = pd.Timestamp.utcnow()
    df["year_month"] = df["start_time"].dt.to_period("M").astype(str)
    df = df[OUTPUT_COLS]
    return df, loaded, skipped


def write_to_silver(df: pd.DataFrame, full_refresh: bool):
    """Write cleaned DataFrame to Silver Delta table, partitioned by year_month."""
    sdf = spark.createDataFrame(df, schema=SILVER_SCHEMA)

    write_mode = "overwrite"
    overwrite_schema = full_refresh

    writer = (
        sdf.write
        .format("delta")
        .mode(write_mode)
        .partitionBy("year_month")
    )
    if overwrite_schema:
        writer = writer.option("overwriteSchema", "true")

    writer.saveAsTable(SILVER_TABLE)
    logger.info("Written %d rows to %s", len(df), SILVER_TABLE)


def main():
    # Allow full_refresh override via Databricks widget at runtime
    full_refresh = FULL_REFRESH
    try:
        full_refresh = dbutils.widgets.get("full_refresh").lower() == "true"
    except Exception:
        pass

    logger.info("=" * 70)
    logger.info("CPS SILVER CLEANER — mode: %s", "FULL REFRESH" if full_refresh else "INCREMENTAL")
    logger.info("=" * 70)

    bronze_files = list_bronze_files()
    logger.info("Bronze Volume: %d files found", len(bronze_files))

    if full_refresh:
        files_to_process = bronze_files
    else:
        processed = get_processed_files()
        logger.info("Silver table: %d files already processed", len(processed))
        files_to_process = [
            f for f in bronze_files
            if f.rsplit("/", 1)[-1] not in processed
        ]
        logger.info("New files to process: %d", len(files_to_process))

    if not files_to_process:
        logger.info("Nothing to do — Silver is up to date")
        return

    df, loaded, skipped = process_files(files_to_process)

    logger.info("=" * 70)
    logger.info("SUMMARY")
    logger.info("=" * 70)
    logger.info("Files loaded: %d | skipped: %d", len(loaded), len(skipped))
    if skipped:
        logger.warning("Skipped files: %s", json.dumps(skipped, indent=2))

    if df.empty:
        logger.warning("No rows produced after cleaning — check skipped files above")
        return

    write_to_silver(df, full_refresh)
    logger.info("Rows written: %d", len(df))
    logger.info("Months covered: %s", sorted(df["year_month"].unique().tolist()))


if __name__ == "__main__":
    main()
