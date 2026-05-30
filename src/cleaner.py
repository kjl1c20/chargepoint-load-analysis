import pandas as pd
import logging

from utils import get_latest_snapshot_id, load_data, save_clean_snapshot


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)

# ============================================================
# data loading
# ============================================================

snapshot_id = get_latest_snapshot_id()
df = load_data("raw", snapshot_id)

rows_in = len(df)
logger.info("Raw snapshot loaded | Rows: %s", f"{rows_in:,}")

cleaning_steps = []

# ============================================================
# type conversion
# ============================================================

df["start_time"] = pd.to_datetime(df["start_time"])
df["end_time"] = pd.to_datetime(df["end_time"])
df["duration"] = pd.to_numeric(df["duration"], errors="coerce")
df["duration_minutes"] = df["duration"] / 60
df["consumption_kwh"] = pd.to_numeric(df["consumption_kwh"], errors="coerce")
df["Power_kW"] = pd.to_numeric(df["Power_kW"], errors="coerce")
df["PricePerKWh"] = pd.to_numeric(df["PricePerKWh"], errors="coerce")
df["connector_id"] = pd.to_numeric(df["connector_id"], errors="coerce").astype("Int64")
df["latitude"] = pd.to_numeric(df["latitude"], errors="coerce")
df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")

# ============================================================
# string normalisation
# ============================================================

cities_before = set(df["City"].dropna().unique())
df["City"] = df["City"].str.strip().str.title()
cities_after = set(df["City"].dropna().unique())

df["Postcode"] = df["Postcode"].str.strip().str.upper()
df["connector_type"] = df["connector_type"].str.strip().str.title()
df["site"] = df["site"].str.strip().str.title()
df["cp_id"] = df["cp_id"].str.strip()

cleaning_steps.append({
    "step": "string_normalisation",
    "city_variants_collapsed": len(cities_before) - len(cities_after)
})

# ============================================================
# deduplication
# ============================================================

before = len(df)
df = df.drop_duplicates()
dropped = before - len(df)

cleaning_steps.append({
    "step": "drop_duplicates",
    "rows_before": before,
    "rows_after": len(df),
    "dropped": dropped
})

logger.info("Duplicates removed | Dropped: %s", f"{dropped:,}")

# ============================================================
# invalid session filter
# ============================================================

before = len(df)
df = df[
    (df["consumption_kwh"] > 0)
    & (df["duration_minutes"] > 1)
    & (df["end_time"] > df["start_time"])
]

cleaning_steps.append({
    "step": "invalid_session_filter",
    "rows_before": before,
    "rows_after": len(df),
    "dropped": before - len(df)
})

# ============================================================
# drop missing coordinates / city
# ============================================================

before = len(df)
df = df.dropna(subset=["City", "latitude", "longitude"])

cleaning_steps.append({
    "step": "drop_missing_city_coords",
    "rows_before": before,
    "rows_after": len(df),
    "dropped": before - len(df)
})

logger.info("Cleaning complete | Rows remaining: %s", f"{len(df):,}")

# ============================================================
# save
# ============================================================

cleaning_report = {
    "rows_in": rows_in,
    "rows_out": len(df),
    "total_dropped": rows_in - len(df),
    "steps": cleaning_steps
}

save_clean_snapshot(df, snapshot_id, cleaning_report)
