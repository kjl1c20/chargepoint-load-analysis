"""Build the Demand-Pressure Index: weighted percentile rank of saturation and utilisation per LA."""

import logging
from pathlib import Path

import pandas as pd

from features import build_cp_metrics, aggregate_to_la


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)

CLEAN_PATH = Path("./data/clean/cps_sessions_clean.parquet")
CP_TABLE_PATH = Path("./data/reference/charge_points.parquet")
OUT_PATH = Path("./data/processed/pressure_index.parquet")

# Saturation (queuing) is the most direct evidence of unmet demand, so it leads;
# utilisation captures overall busy-ness. Tunable; need not sum to 1.
PRESSURE_WEIGHTS = {
    "saturation_rate": 0.6,
    "utilisation": 0.4
}


def build_pressure_index(la_metrics, weights=PRESSURE_WEIGHTS):
    """Add a 0–1 pressure_score and rank, via weighted percentile ranks."""
    df = la_metrics.copy()

    weighted_sum = 0.0
    for col, w in weights.items():
        df[f"{col}_pct"] = df[col].rank(pct=True)
        weighted_sum = weighted_sum + w * df[f"{col}_pct"]

    df["pressure_score"] = weighted_sum / sum(weights.values())
    df["pressure_rank"] = df["pressure_score"].rank(ascending=False, method="min").astype(int)
    return df.sort_values("pressure_score", ascending=False).reset_index(drop=True)


if __name__ == "__main__":
    sessions = pd.read_parquet(CLEAN_PATH)
    charge_points = pd.read_parquet(CP_TABLE_PATH)

    cp_metrics = build_cp_metrics(sessions, charge_points)
    la_metrics = aggregate_to_la(cp_metrics, charge_points)
    index = build_pressure_index(la_metrics)

    logger.info("Demand-Pressure Index built | %s local authorities", len(index))
    logger.info(
        "\n%s",
        index[[
            "pressure_rank", "local_authority", "pressure_score",
            "utilisation", "saturation_rate", "n_connectors", "revenue_per_connector"
        ]].to_string(index=False)
    )

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    index.to_parquet(OUT_PATH, index=False)
    logger.info("Saved | %s", OUT_PATH)
