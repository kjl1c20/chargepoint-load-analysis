"""
Usage-profile clustering — group charge points into demand archetypes.

Each charge point gets a behavioural fingerprint (WHEN it's used, HOW LONG
sessions last, charger type) — all *shape* features that are independent of how
many sessions it had or how long it was in the dataset, so the result is robust
to the CPS network churn (unlike a demand forecast).

K-means then groups chargers into archetypes (e.g. commuter rapid / daytime
destination / overnight). See docs/model-decisions.md (Decision 3).

Output:
  data/processed/cp_clusters.parquet  — per charge point: profile + cluster + label
Run:  poetry run python src/cluster_profiles.py
"""

import logging
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.preprocessing import StandardScaler
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

CLEAN_PATH = Path("./data/clean/cps_sessions_clean.parquet")
CP_TABLE_PATH = Path("./data/reference/charge_points.parquet")
OUT_PATH = Path("./data/processed/cp_clusters.parquet")

MIN_SESSIONS = 30          # need enough sessions for a stable profile
K_RANGE = range(3, 8)      # candidate cluster counts (chosen by silhouette)

# behavioural fingerprint (all shape/per-session, churn-proof)
FEATURES = [
    "pct_morning", "pct_midday", "pct_evening", "pct_overnight",
    "weekend_ratio", "rapid_share", "median_duration_min", "median_energy_kwh"
]


def build_profiles(sessions: pd.DataFrame) -> pd.DataFrame:
    """Per-charge-point behavioural profile features."""
    df = sessions.copy()
    df["start_time"] = pd.to_datetime(df["start_time"])
    hour = df["start_time"].dt.hour

    df["t_morning"] = hour.isin([6, 7, 8, 9])
    df["t_midday"] = hour.isin([10, 11, 12, 13, 14, 15])
    df["t_evening"] = hour.isin([16, 17, 18, 19, 20, 21])
    df["t_overnight"] = hour.isin([22, 23, 0, 1, 2, 3, 4, 5])
    df["is_weekend"] = df["start_time"].dt.dayofweek >= 5
    df["is_rapid"] = df["connector_type"].str.contains("rapid", case=False, na=False)

    feat = (
        df.groupby("cp_id")
          .agg(n_sessions=("cp_id", "size"),
               pct_morning=("t_morning", "mean"),
               pct_midday=("t_midday", "mean"),
               pct_evening=("t_evening", "mean"),
               pct_overnight=("t_overnight", "mean"),
               weekend_ratio=("is_weekend", "mean"),
               rapid_share=("is_rapid", "mean"),
               median_duration_min=("duration_minutes", "median"),
               median_energy_kwh=("consumption_kwh", "median"))
          .reset_index()
    )
    return feat[feat["n_sessions"] >= MIN_SESSIONS].reset_index(drop=True)


def choose_k(X: np.ndarray) -> int:
    """Pick the cluster count with the best silhouette score."""
    best_k, best_score = K_RANGE[0], -1.0
    for k in K_RANGE:
        labels = KMeans(n_clusters=k, random_state=42, n_init=10).fit_predict(X)
        score = silhouette_score(X, labels, sample_size=5000, random_state=42)
        logger.info("  k=%d | silhouette=%.3f", k, score)
        if score > best_score:
            best_k, best_score = k, score
    return best_k


def label_cluster(row: pd.Series) -> str:
    """Heuristic human label from a cluster's centroid profile."""
    when = max(
        [("morning", row["pct_morning"]), ("daytime", row["pct_midday"]),
         ("evening", row["pct_evening"]), ("overnight", row["pct_overnight"])],
        key=lambda t: t[1]
    )[0]
    # flag a distinctly overnight-heavy cluster even if daytime edges it
    if row["pct_overnight"] >= 0.20:
        when = "overnight"

    speed = "Rapid" if row["rapid_share"] >= 0.5 else "AC"

    d = row["median_duration_min"]
    if d < 60:
        stay = "top-up"
    elif d < 240:
        stay = "medium-stay"
    elif d < 600:
        stay = "long-stay"
    else:
        stay = "all-day"

    return f"{speed} {stay} ({when})"


# ============================================================
# run
# ============================================================

if __name__ == "__main__":
    sessions = pd.read_parquet(
        CLEAN_PATH,
        columns=["cp_id", "connector_type", "duration_minutes", "consumption_kwh", "start_time"]
    )
    profiles = build_profiles(sessions)
    logger.info("Profiled %s charge points (>= %d sessions)", f"{len(profiles):,}", MIN_SESSIONS)

    X = StandardScaler().fit_transform(profiles[FEATURES])
    k = choose_k(X)
    logger.info("Chosen k = %d", k)

    profiles["cluster"] = KMeans(n_clusters=k, random_state=42, n_init=10).fit_predict(X)

    # profile each cluster + attach a readable archetype label
    centroids = profiles.groupby("cluster")[FEATURES].mean()
    centroids["n_charge_points"] = profiles.groupby("cluster").size()
    centroids["archetype"] = centroids.apply(label_cluster, axis=1)
    profiles["archetype"] = profiles["cluster"].map(centroids["archetype"])

    logger.info("\nCluster archetypes:\n%s", centroids.round(2).to_string())

    # attach geography for planning (which archetypes dominate each LA)
    cps = pd.read_parquet(CP_TABLE_PATH, columns=["cp_id", "local_authority"])
    profiles = profiles.merge(cps, on="cp_id", how="left")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    profiles.to_parquet(OUT_PATH, index=False)
    logger.info("Saved | %s", OUT_PATH)
