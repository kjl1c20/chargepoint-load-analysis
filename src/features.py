"""
Demand-pressure engine (CPS schema).

Turns cleaned ChargePlace Scotland sessions into demand-pressure metrics in two
stages, so the heavy session math stays independent of geography:

  1. build_cp_metrics(sessions)         -> one row per charge point (cp_id)
  2. aggregate_to_la(cp_metrics, cps)   -> roll up to local authority via the
                                           charge point table (cp_id -> LA)

Metrics:
  - utilisation : occupied connector-hours / available connector-hours
  - saturation  : share of time a charge point is completely full (all
                  connectors busy at once) -> queuing / unmet demand
  - revenue     : sum of `amount` (£ paid) -> commercial value

These feed the Demand-Pressure Index (pressure_index.py).
"""

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


def build_cp_metrics(sessions: pd.DataFrame) -> pd.DataFrame:
    """Per-charge-point demand metrics from clean CPS sessions (no geography needed)."""
    df = sessions.copy()
    df["start_time"] = pd.to_datetime(df["start_time"])
    df["end_time"] = pd.to_datetime(df["end_time"])
    df["occupied_hours"] = (df["end_time"] - df["start_time"]).dt.total_seconds() / 3600.0
    df["connector_key"] = df["cp_id"].astype(str) + "_" + df["connector"].astype(str)

    window_end = df["end_time"].max()
    logger.info("Window end %s | %s sessions across %s charge points",
                window_end.date(), f"{len(df):,}", f"{df['cp_id'].nunique():,}")

    # ---- per-connector availability (first-seen -> window_end) ----
    conn = (
        df.groupby(["cp_id", "connector_key"])
          .agg(occupied_hours=("occupied_hours", "sum"), first_seen=("start_time", "min"))
          .reset_index()
    )
    conn["available_hours"] = (window_end - conn["first_seen"]).dt.total_seconds() / 3600.0
    cp_util = (
        conn.groupby("cp_id")
            .agg(occupied_hours=("occupied_hours", "sum"),
                 available_connector_hours=("available_hours", "sum"),
                 n_connectors=("connector_key", "nunique"))
            .reset_index()
    )

    # ---- saturation per charge point ----
    sat_rows = []
    for cp, g in df.groupby("cp_id", sort=False):
        k = g["connector"].nunique()
        starts = g["start_time"].values.astype("datetime64[s]").astype("int64")
        ends = g["end_time"].values.astype("datetime64[s]").astype("int64")
        cp_avail = (window_end - g["start_time"].min()).total_seconds() / 3600.0
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
