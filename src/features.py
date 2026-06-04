"""Per-charge-point demand metrics (utilisation, saturation, revenue) rolled up to local authority."""

import numpy as np
import pandas as pd
import logging

logger = logging.getLogger(__name__)


def _saturated_hours(starts: np.ndarray, ends: np.ndarray, k: int) -> float:
    """Hours with >= k concurrent sessions (all k connectors busy), via sweep line."""
    if k <= 0 or len(starts) == 0:
        return 0.0
    times = np.concatenate([starts, ends])
    deltas = np.concatenate([np.ones(len(starts), dtype=np.int64),
                             -np.ones(len(ends), dtype=np.int64)])
    order = np.lexsort((deltas, times))  # ties: ends (-1) before starts (+1)
    times, deltas = times[order], deltas[order]

    saturated, concurrency = 0, 0
    for i in range(len(times)):
        if i > 0 and concurrency >= k:
            saturated += times[i] - times[i - 1]
        concurrency += deltas[i]
    return saturated / 3600.0


def build_cp_metrics(sessions: pd.DataFrame, charge_points: pd.DataFrame = None) -> pd.DataFrame:
    """Per-charge-point demand metrics from clean CPS sessions (no geography needed)."""
    df = sessions.copy()
    df["start_time"] = pd.to_datetime(df["start_time"])
    df["end_time"] = pd.to_datetime(df["end_time"])
    df["occupied_hours"] = (df["end_time"] - df["start_time"]).dt.total_seconds() / 3600.0
    df["connector_key"] = df["cp_id"].astype(str) + "_" + df["connector"].astype(str)

    window_end = df["end_time"].max()
    logger.info("Window end %s | %s sessions across %s charge points",
                window_end.date(), f"{len(df):,}", f"{df['cp_id'].nunique():,}")

    # OCM-derived connector counts where available; fall back to session-observed
    if charge_points is not None and "n_connectors" in charge_points.columns:
        ocm_k = charge_points.set_index("cp_id")["n_connectors"].to_dict()
    else:
        ocm_k = {}
    ocm_count = sum(1 for cp in df["cp_id"].unique() if cp in ocm_k)
    logger.info("OCM connector counts available for %d / %d charge points",
                ocm_count, df["cp_id"].nunique())

    # ---- per-connector availability (first-seen -> last-seen, not window_end) ----
    # Capping at last_seen avoids counting dead time after a connector migrates off CPS.
    conn = (
        df.groupby(["cp_id", "connector_key"])
          .agg(occupied_hours=("occupied_hours", "sum"),
               first_seen=("start_time", "min"),
               last_seen=("end_time", "max"))
          .reset_index()
    )
    conn["available_hours"] = (conn["last_seen"] - conn["first_seen"]).dt.total_seconds() / 3600.0
    cp_util = (
        conn.groupby("cp_id")
            .agg(occupied_hours=("occupied_hours", "sum"),
                 available_connector_hours=("available_hours", "sum"),
                 n_connectors_observed=("connector_key", "nunique"))
            .reset_index()
    )
    # use max(OCM, session-observed): sessions prove the lower bound (if 4 connector IDs
    # were used, at least 4 exist); OCM may know about connectors that were never used.
    cp_util["n_connectors"] = cp_util.apply(
        lambda r: max(ocm_k.get(r["cp_id"], 0), r["n_connectors_observed"]), axis=1
    )

    # ---- saturation per charge point ----
    sat_rows = []
    for cp, g in df.groupby("cp_id", sort=False):
        k = max(ocm_k.get(cp, 0), g["connector"].nunique())
        starts = g["start_time"].values.astype("datetime64[s]").astype("int64")
        ends = g["end_time"].values.astype("datetime64[s]").astype("int64")
        cp_avail = (g["end_time"].max() - g["start_time"].min()).total_seconds() / 3600.0
        sat_rows.append({"cp_id": cp,
                         "saturated_hours": _saturated_hours(starts, ends, k),
                         "cp_available_hours": cp_avail})
    cp_sat = pd.DataFrame(sat_rows)

    # ---- core totals (revenue = amount paid) ----
    cp_core = (
        df.groupby("cp_id")
          .agg(total_sessions=("connector", "count"),
               total_energy_kwh=("consumption_kwh", "sum"),
               total_revenue=("amount", "sum"))
          .reset_index()
    )

    cp = cp_core.merge(cp_util, on="cp_id").merge(cp_sat, on="cp_id")
    return cp


def aggregate_to_la(cp_metrics: pd.DataFrame, charge_points: pd.DataFrame) -> pd.DataFrame:
    """Roll charge-point metrics up to local authority via the charge point table."""
    geo_cols = ["cp_id", "local_authority", "latitude", "longitude"]
    m = cp_metrics.merge(charge_points[geo_cols], on="cp_id", how="left")

    unmatched = m["local_authority"].isna().sum()
    if unmatched:
        logger.warning("%s charge points have no local authority — excluded", f"{unmatched:,}")
    m = m.dropna(subset=["local_authority"])

    la = (
        m.groupby("local_authority")
         .agg(n_chargepoints=("cp_id", "nunique"),
              n_connectors=("n_connectors", "sum"),
              total_sessions=("total_sessions", "sum"),
              total_energy_kwh=("total_energy_kwh", "sum"),
              total_revenue=("total_revenue", "sum"),
              occupied_hours=("occupied_hours", "sum"),
              available_connector_hours=("available_connector_hours", "sum"),
              saturated_hours=("saturated_hours", "sum"),
              cp_available_hours=("cp_available_hours", "sum"),
              latitude=("latitude", "mean"),
              longitude=("longitude", "mean"))
         .reset_index()
    )

    la["utilisation"] = (la["occupied_hours"] / la["available_connector_hours"]).clip(upper=1.0)
    la["saturation_rate"] = la["saturated_hours"] / la["cp_available_hours"]
    la["revenue_per_connector"] = la["total_revenue"] / la["n_connectors"]
    return la
