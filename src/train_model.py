from pathlib import Path
import pandas as pd
import numpy as np
import logging

from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report, roc_auc_score

import xgboost as xgb

from utils import get_latest_snapshot_id, load_data, PROCESSED_DATA_DIR


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)

# ============================================================
# data loading
# ============================================================

snapshot_id = get_latest_snapshot_id() # HARD CODE SNAPSHOT ID IN STRING FOR HISTORICAL DATA
df = load_data('clean', snapshot_id)

# ============================================================
# feature engineering
# ============================================================

df["hour"] = df["start_time"].dt.hour
df["dayofweek"] = df["start_time"].dt.dayofweek
df["month"] = df["start_time"].dt.month
df["is_weekend"] = (
    df["dayofweek"] >= 5
).astype(int)


# Used city for clustering
df["cluster"] = df["City"]


# connector_id approximates actual charging slots.
connectors_per_cluster = (
    df.groupby("cluster")["connector_id"]
      .nunique()
      .reset_index(name="num_connectors")
)


# aggregare based on cluster
cluster_features = (
    df.groupby("cluster")
      .agg(
          total_sessions=("connector_id", "count"),

          unique_chargepoints=("cp_id", "nunique"),

          avg_session_duration=("duration_minutes", "mean"),

          median_session_duration=("duration_minutes", "median"),

          total_energy_kwh=("consumption_kwh", "sum"),

          avg_energy_kwh=("consumption_kwh", "mean"),

          avg_power_kw=("Power_kW", "mean"),

          unique_postcodes=("Postcode", "nunique"),

          peak_hour=("hour", lambda x: x.mode()[0]),

          weekend_ratio=("is_weekend", "mean"),

          latitude=("latitude", "mean"),

          longitude=("longitude", "mean")
      )
      .reset_index()
)

# Merge connector capacity
cluster_features = cluster_features.merge(
    connectors_per_cluster,
    on="cluster",
    how="left"
)


# utilisation logic
cluster_features["sessions_per_connector"] = (
    cluster_features["total_sessions"]
    / cluster_features["num_connectors"]
)

cluster_features["energy_per_connector"] = (
    cluster_features["total_energy_kwh"]
    / cluster_features["num_connectors"]
)


# ============================================================
# target label defintion
# ============================================================

# IMPORTANT:
# create a proxy target using demand pressure.
# High utilisation regions are labelled as:
# 1 = needs more chargers

UTILISATION_THRESHOLD = (
    cluster_features["sessions_per_connector"]
    .quantile(0.75)
)

cluster_features["label"] = np.where(
    cluster_features["sessions_per_connector"]
    > UTILISATION_THRESHOLD,
    1,
    0
)

logger.info(f"\nLabel Distribution:{cluster_features["label"].value_counts()}")

# ============================================================
# machine learning model set up
# ============================================================

FEATURE_COLUMNS = [
    "total_sessions",
    "unique_chargepoints",
    "avg_session_duration",
    "median_session_duration",
    "total_energy_kwh",
    "avg_energy_kwh",
    "avg_power_kw",
    "unique_postcodes",
    "peak_hour",
    "weekend_ratio",
    "num_connectors",
    "sessions_per_connector",
    "energy_per_connector"
]

X = cluster_features[FEATURE_COLUMNS]
y = cluster_features["label"]


X_train, X_test, y_train, y_test = train_test_split(
    X,
    y,
    test_size=0.2,
    random_state=42,
    stratify=y
)


# ============================================================
# training
# ============================================================

model = xgb.XGBClassifier(
    n_estimators=200,
    max_depth=5,
    learning_rate=0.05,
    subsample=0.8,
    colsample_bytree=0.8,
    random_state=42,
    eval_metric="logloss"
)

model.fit(X_train, y_train)


# ============================================================
# model evaluation
# ============================================================

preds = model.predict(X_test)
probs = model.predict_proba(X_test)[:, 1]

logger.info("\n==============================")
logger.info("CLASSIFICATION REPORT")
logger.info("==============================\n")

logger.info(classification_report(y_test, preds))

logger.info(f"\nROC-AUC:{roc_auc_score(y_test, probs)}")

# ============================================================
# results
# ============================================================

cluster_features["need_probability"] = (
    model.predict_proba(X)[:, 1]
)

cluster_features = cluster_features.sort_values(
    by="need_probability",
    ascending=False
)

# ============================================================
# 11. SAVE RESULTS
# ============================================================
result_path = (PROCESSED_DATA_DIR
                / f"{snapshot_id}_result.parquet"
                )
cluster_features.to_parquet(
    result_path,
    index=False
)

logger.info("Model results saved")