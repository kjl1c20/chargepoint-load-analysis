"""Combine, clean and write all raw CPS session files to a single parquet."""

import glob
import json
import logging
from pathlib import Path
from datetime import datetime

import pandas as pd

from harvest_cps import _normalise


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)

RAW_CPS_DIR = Path("./data/raw_cps")
CLEAN_DATA_DIR = Path("./data/clean")
METADATA_DIR = Path("./data/metadata")


def _read_any(path: str) -> pd.DataFrame:
    """Read a session file as csv (with encoding fallback) or xlsx."""
    if path.lower().endswith(".csv"):
        for enc in ("utf-8", "latin-1"):
            try:
                return pd.read_csv(path, encoding=enc)
            except UnicodeDecodeError:
                continue
        return pd.read_csv(path, encoding="latin-1", engine="python")
    return pd.read_excel(path)


def _fix_blank_duration(df: pd.DataFrame) -> pd.DataFrame:
    """Some files export the duration column with a blank header ('Unnamed: N')."""
    lower = [str(c).strip().lower() for c in df.columns]
    if "duration" not in lower:
        unnamed = [c for c in df.columns if str(c).strip().lower().startswith("unnamed")]
        if len(unnamed) == 1:
            df = df.rename(columns={unnamed[0]: "duration"})
    return df


# load and combine raw files

cleaning_steps = []

frames, loaded, skipped = [], [], []
for path in sorted(glob.glob(str(RAW_CPS_DIR / "*"))):
    if not path.lower().endswith((".csv", ".xlsx")):
        continue
    name = Path(path).name
    try:
        raw = _fix_blank_duration(_read_any(path))
        frames.append(_normalise(raw, name))
        loaded.append(name)
    except Exception as e:
        skipped.append({"file": name, "reason": str(e)})
        logger.warning("Skipped %s: %s", name, e)

df = pd.concat(frames, ignore_index=True)
rows_in = len(df)
logger.info("Loaded %d files | %d skipped | %s raw rows", len(loaded), len(skipped), f"{rows_in:,}")

cleaning_steps.append({
    "step": "load_and_combine",
    "files_loaded": len(loaded),
    "files_skipped": len(skipped),
    "skipped_detail": skipped,
    "rows_in": rows_in
})

# drop duplicates (same session appears across overlapping monthly files)

before = len(df)
df = df.drop_duplicates(subset=["cp_id", "connector", "start_time", "consumption_kwh"])
cleaning_steps.append({
    "step": "drop_duplicates",
    "rows_before": before,
    "rows_after": len(df),
    "dropped": before - len(df)
})
logger.info("Duplicates removed | dropped %s", f"{before - len(df):,}")

# normalise strings

df["site_name"] = df["site_name"].str.strip().str.title()
df["connector_type"] = df["connector_type"].str.strip().str.title()
df["cp_id"] = df["cp_id"].str.strip()
cleaning_steps.append({"step": "string_normalisation"})

# filter out invalid sessions
# Upper duration bound: a session blocking a connector for > 24h is implausible
# (abandoned plug / meter error). Some duration values are wildly corrupt
# (end-times decades in the future), which would otherwise wreck utilisation.
MAX_DURATION_MINUTES = 24 * 60
MAX_CONSUMPTION_KWH = 300   # physically impossible for one EV session to exceed this

before = len(df)
df = df[
    (df["consumption_kwh"] > 0)
    & (df["consumption_kwh"] <= MAX_CONSUMPTION_KWH)
    & (df["duration_minutes"] > 1)
    & (df["duration_minutes"] <= MAX_DURATION_MINUTES)
    & (df["end_time"] > df["start_time"])
]
cleaning_steps.append({
    "step": "invalid_session_filter",
    "rows_before": before,
    "rows_after": len(df),
    "dropped": before - len(df),
    "max_duration_minutes": MAX_DURATION_MINUTES,
    "max_consumption_kwh": MAX_CONSUMPTION_KWH
})
logger.info("Invalid sessions removed | dropped %s", f"{before - len(df):,}")

# save

df["month"] = df["start_time"].dt.to_period("M").astype(str)
df = df.sort_values("start_time").reset_index(drop=True)

CLEAN_DATA_DIR.mkdir(parents=True, exist_ok=True)
METADATA_DIR.mkdir(parents=True, exist_ok=True)

clean_path = CLEAN_DATA_DIR / "cps_sessions_clean.parquet"
df.to_parquet(clean_path, index=False)

report = {
    "created_at": datetime.now().isoformat(),
    "source_dir": str(RAW_CPS_DIR),
    "rows_in": rows_in,
    "rows_out": len(df),
    "total_dropped": rows_in - len(df),
    "charge_points": int(df["cp_id"].nunique()),
    "month_coverage": sorted(df["month"].unique().tolist()),
    "steps": cleaning_steps
}
with open(METADATA_DIR / "cps_sessions_clean_report.json", "w") as f:
    json.dump(report, f, indent=4)

logger.info("Clean data saved | %s rows -> %s", f"{len(df):,}", clean_path)
logger.info("Month coverage: %s -> %s (%d months)",
            report["month_coverage"][0], report["month_coverage"][-1], len(report["month_coverage"]))
