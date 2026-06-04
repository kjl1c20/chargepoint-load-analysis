"""
Build the charge point dimension table.

One row per cp_id, combining:
  - session-derived attributes (site, connectors, activity window, totals)
  - geocoded location (lat/long, local authority, postcode) from site_name

Output: data/reference/charge_points.parquet  — joins onto the sessions fact
table by cp_id to give every session a location.

Run:  poetry run python src/build_cp_table.py   (geocodes via cache; see geocode_sites.py)
"""

import logging
from pathlib import Path

import pandas as pd

from geocode_sites import geocode_all


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)

CLEAN_PATH = Path("./data/clean/cps_sessions_clean.parquet")
OUT_PATH = Path("./data/reference/charge_points.parquet")


def _mode(s: pd.Series):
    m = s.mode()
    return m.iloc[0] if not m.empty else None


# ============================================================
# session-derived attributes per charge point
# ============================================================

clean = pd.read_parquet(
    CLEAN_PATH,
    columns=["cp_id", "site_name", "connector", "connector_type"]
)

cp = (
    clean.groupby("cp_id")
         .agg(
             site_name=("site_name", _mode),
             connector_type=("connector_type", _mode),
             n_connectors=("connector", "nunique")
         )
         .reset_index()
)
logger.info("Charge points: %s | unique sites: %s",
            f"{len(cp):,}", f"{cp['site_name'].nunique():,}")

# ============================================================
# geocode site names -> location, join on
# ============================================================

cache = geocode_all(cp["site_name"].dropna().unique().tolist())

geo = pd.DataFrame.from_dict(cache, orient="index")
geo.index.name = "site_name"
geo = geo.reset_index()

cp = cp.merge(
    geo[["site_name", "latitude", "longitude", "local_authority", "postcode", "geocode_method"]],
    on="site_name", how="left"
)

# ============================================================
# save + report coverage
# ============================================================

OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
cp.to_parquet(OUT_PATH, index=False)

with_la = cp["local_authority"].notna().sum()
logger.info("Charge point table saved | %s", OUT_PATH)
logger.info("Local authority resolved | %s/%s charge points (%.0f%%)",
            f"{with_la:,}", f"{len(cp):,}", 100 * with_la / len(cp))
