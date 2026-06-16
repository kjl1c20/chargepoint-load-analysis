"""Saturation helper for the Spark site-pressure job.

The saturation metric is an imperative sweep-line over each charge point's sorted
session events — it doesn't translate to Spark SQL cleanly, so it stays in pandas and
runs distributed via groupBy("cp_id").applyInPandas(compute_saturation, SAT_SCHEMA).
Everything else (availability windows, totals, ranking) is native Spark in site_pressure.py.
"""

import numpy as np
import pandas as pd

# Charge points below this session count are too noisy to rank reliably.
MIN_SESSIONS_SITE = 100

# Saturation (queuing) leads; utilisation captures overall busy-ness. Tunable.
PRESSURE_WEIGHTS = {"saturation_rate": 0.6, "utilisation": 0.4}

# applyInPandas output schema (Spark DDL string).
SAT_SCHEMA = "cp_id string, saturated_hours double, cp_available_hours double"


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


def compute_saturation(pdf: pd.DataFrame) -> pd.DataFrame:
    """applyInPandas UDF: per cp_id, hours at >= n_connectors concurrent sessions.

    Receives all sessions for one cp_id (columns: cp_id, start_time, end_time,
    n_connectors). k = n_connectors (from Silver charge_points). Returns one row.
    """
    cp_id = pdf["cp_id"].iloc[0]
    k_val = pdf["n_connectors"].iloc[0]
    k = int(k_val) if pd.notna(k_val) else 0

    starts = pdf["start_time"].values.astype("datetime64[s]").astype("int64")
    ends = pdf["end_time"].values.astype("datetime64[s]").astype("int64")
    saturated = _saturated_hours(starts, ends, k)
    available = (pdf["end_time"].max() - pdf["start_time"].min()).total_seconds() / 3600.0

    return pd.DataFrame([{
        "cp_id": cp_id,
        "saturated_hours": float(saturated),
        "cp_available_hours": float(available),
    }])
