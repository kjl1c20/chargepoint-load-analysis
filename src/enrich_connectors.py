"""Enrich charge_points.parquet with real connector counts from Open Charge Map."""

import os
import logging
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv
from scipy.spatial import KDTree

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

OCM_URL = "https://api.openchargemap.io/v3/poi/"
# Scotland centre + radius covers the whole country in one request.
# The boundingbox param is ignored by the OCM API server-side.
SCOTLAND_LAT, SCOTLAND_LON, SCOTLAND_RADIUS_KM = 57.0, -4.0, 350
LAT_MIN, LAT_MAX = 54.5, 61.0
LON_MIN, LON_MAX = -9.0, -0.5
MATCH_RADIUS_M = 200

CP_TABLE_PATH = Path("./data/reference/charge_points.parquet")
OCM_CACHE_PATH = Path("./data/reference/ocm_scotland.parquet")

EARTH_R = 6_371_000


def fetch_ocm_scotland(api_key: str) -> pd.DataFrame:
    """Fetch OCM POIs within 350km of Scotland's centre (single request, no pagination needed)."""
    resp = requests.get(OCM_URL, params={
        "key": api_key,
        "latitude": SCOTLAND_LAT,
        "longitude": SCOTLAND_LON,
        "distance": SCOTLAND_RADIUS_KM,
        "distanceunit": "km",
        "maxresults": 10000,
        "output": "json",
        "compact": True,
    }, timeout=60)
    resp.raise_for_status()
    pois = resp.json()
    logger.info("OCM returned %d POIs within %dkm of Scotland centre", len(pois), SCOTLAND_RADIUS_KM)

    rows = []
    for p in pois:
        addr = p.get("AddressInfo", {})
        lat, lon = addr.get("Latitude"), addr.get("Longitude")
        if lat is None or lon is None:
            continue
        if not (LAT_MIN <= float(lat) <= LAT_MAX and LON_MIN <= float(lon) <= LON_MAX):
            continue
        conns = p.get("Connections") or []
        n_connections = sum(c.get("Quantity") or 1 for c in conns)
        n_points = p.get("NumberOfPoints") or 1
        rows.append({
            "ocm_id": p["ID"],
            "ocm_title": addr.get("Title"),
            "ocm_lat": float(lat),
            "ocm_lon": float(lon),
            "ocm_n_connections": n_connections,
            "ocm_n_points": int(n_points),
            "ocm_connectors_per_point": max(1, round(n_connections / n_points)),
        })
    logger.info("Scotland-filtered: %d POIs", len(rows))
    return pd.DataFrame(rows)


def _to_xyz(lat_deg: np.ndarray, lon_deg: np.ndarray) -> np.ndarray:
    """Convert lat/lon (degrees) to 3D unit-sphere coordinates for KDTree."""
    lat = np.radians(lat_deg)
    lon = np.radians(lon_deg)
    return np.column_stack([
        np.cos(lat) * np.cos(lon),
        np.cos(lat) * np.sin(lon),
        np.sin(lat),
    ])


def spatial_join(cp: pd.DataFrame, ocm: pd.DataFrame, radius_m: float) -> pd.DataFrame:
    """
    Nearest-neighbour join: each CPS charge point → closest OCM POI within radius_m.
    Returns cp with added ocm_id and ocm_connectors_per_point columns (NaN if no match).
    """
    cp_geo = cp.dropna(subset=["latitude", "longitude"])

    ocm_xyz = _to_xyz(ocm["ocm_lat"].values, ocm["ocm_lon"].values)
    cp_xyz  = _to_xyz(cp_geo["latitude"].values, cp_geo["longitude"].values)

    # chord distance on unit sphere for the given radius
    threshold = 2 * np.sin(radius_m / (2 * EARTH_R))

    tree = KDTree(ocm_xyz)
    dists, idxs = tree.query(cp_xyz, k=1, distance_upper_bound=threshold)

    matched = idxs < len(ocm)
    safe_idx = np.where(matched, idxs, 0)

    cp_geo = cp_geo.copy()
    cp_geo["ocm_id"] = np.where(matched, ocm["ocm_id"].values[safe_idx], pd.NA)
    cp_geo["ocm_connectors_per_point"] = np.where(
        matched,
        ocm["ocm_connectors_per_point"].values[safe_idx].astype(float),
        np.nan,
    )

    logger.info(
        "Spatial match: %d / %d charge points matched to an OCM POI within %dm",
        matched.sum(), len(cp_geo), radius_m,
    )

    return cp.merge(cp_geo[["cp_id", "ocm_id", "ocm_connectors_per_point"]], on="cp_id", how="left")


if __name__ == "__main__":
    api_key = os.getenv("OCM_API_KEY")
    if not api_key:
        raise RuntimeError("OCM_API_KEY not found — set it in .env")

    # fetch or load cached OCM data
    if OCM_CACHE_PATH.exists():
        logger.info("Loading cached OCM data from %s", OCM_CACHE_PATH)
        ocm = pd.read_parquet(OCM_CACHE_PATH)
        # cache may contain all-GB data; filter to Scotland
        ocm = ocm[
            ocm["ocm_lat"].between(LAT_MIN, LAT_MAX) &
            ocm["ocm_lon"].between(LON_MIN, LON_MAX)
        ].reset_index(drop=True)
        logger.info("Scotland-filtered OCM POIs: %d", len(ocm))
    else:
        logger.info("Fetching OCM POIs for Scotland...")
        ocm = fetch_ocm_scotland(api_key)
        OCM_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        ocm.to_parquet(OCM_CACHE_PATH, index=False)
        logger.info("Saved %d OCM POIs → %s", len(ocm), OCM_CACHE_PATH)

    logger.info("OCM: %d POIs | %d total connections", len(ocm), ocm["ocm_n_connections"].sum())

    cp = pd.read_parquet(CP_TABLE_PATH)
    cp = spatial_join(cp, ocm, radius_m=MATCH_RADIUS_M)

    # replace session-derived n_connectors with OCM value where matched
    n_matched = cp["ocm_connectors_per_point"].notna().sum()
    old_mean = cp["n_connectors"].mean()
    cp["n_connectors"] = (
        cp["ocm_connectors_per_point"]
        .combine_first(cp["n_connectors"].astype(float))
        .round()
        .astype(int)
    )
    logger.info(
        "n_connectors updated: %d from OCM, %d kept session-derived | mean %.2f → %.2f",
        n_matched, len(cp) - n_matched, old_mean, cp["n_connectors"].mean(),
    )

    cp.to_parquet(CP_TABLE_PATH, index=False)
    logger.info("Saved enriched charge_points.parquet")
