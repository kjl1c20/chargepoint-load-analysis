# Scotland EV Charging Infrastructure Analysis

## Overview

When I discovered the UK's public EV charging network might not be keeping pace with rising EV adoption, I wanted to find out if there was enough public infrastructure to meet demand. ChargePlace Scotland publishes real session level data, which gave me the chance to investigate. I used clustering to surface charging patterns across Scotland and identify which regions are facing the most pressure.

Two complementary outputs answer the question:

1. A **Demand-Pressure Index** — ranks local authorities by infrastructure strain (saturation + utilisation), fully transparent and explainable.
2. **Usage-Profile Clustering** — groups charge points into behavioural archetypes (rapid top-up, workplace AC, overnight residential, etc.) to guide *what type* of capacity to add where.

---

## Data Source

**ChargePlace Scotland public session data** — monthly spreadsheets published at [chargeplacescotland.org](https://chargeplacescotland.org/monthly-charge-point-performance/)

- **Period:** January 2024 – April 2026 (28 months)
- **Sessions:** ~3.16 million (after cleaning)
- **Charge points:** 5,325 unique charge point IDs

> **Network fragmentation note:** CPS is handing chargers to other operators through 2025–26, so the dataset shrinks over time. Falling total session counts reflect dataset coverage loss, not declining demand — demand per charger is flat (~36–40 sessions/month). For this reason, no demand-volume forecast is published.

---

## Methodology

### Pipeline

```
data/raw_cps/          ← manually downloaded CPS monthly xlsx/csv files
    ↓ src/cleaner_cps.py
data/clean/cps_sessions_clean.parquet
    ↓ src/geocode_sites.py + src/build_cp_table.py
data/reference/charge_points.parquet   ← cp_id → local authority (via Nominatim geocoding)
    ↓ src/pressure_index.py
data/processed/pressure_index.parquet  ← LA-level demand pressure ranking
    ↓ src/cluster_profiles.py
data/processed/cp_clusters.parquet     ← per charge point behavioural archetype
    ↓ src/dashboard.py
Streamlit dashboard (5 tabs)
```

### Demand-Pressure Index

Percentile-ranked composite of two signals per local authority:
- **Saturation rate** (weight 0.6) — share of time when all connectors at a charge point are simultaneously busy (queuing pressure)
- **Utilisation** (weight 0.4) — share of available connector-time that is occupied

Revenue is reported separately as a commercial lens, never folded into the pressure score.

### Usage-Profile Clustering (ML deliverable)

Each charge point (≥ 30 sessions) is represented by 8 shape features:
- Time-of-day shares (morning / midday / evening / overnight)
- Weekend ratio, rapid-connector share
- Median session duration, median energy consumed

StandardScaler + KMeans (k=6, chosen by silhouette score = 0.32) produces 6 archetypes:

| Archetype | Signature |
|---|---|
| Rapid top-up (daytime) | 43 min, 96% rapid — en-route / quick top-up |
| AC medium-stay (daytime) | ~3 h — shopping / destination |
| AC long-stay (morning) | ~4.5 h, morning peak — workplace |
| AC long-stay (evening) | ~7 h — evening / residential |
| AC all-day (daytime) | ~15 h — park-and-ride |
| AC long-stay (overnight) | overnight-heavy — residential |

---

## Why the v1 (SENSE-based) approach was retired

The original version used data from the Smart Energy Data Service (SDR-SENSE). It was retired in June 2026 for two reasons:

- **Incomplete data.** SENSE only exposed two non-consecutive months of CPS sessions (Sep 2024 and Oct 2025) — far too little for any reliable ML study.
- **ML proved unnecessary.** The v1 classifier had target leakage (the label was derived from the same signal used as a feature). Once fixed, a transparent ranking performed equally well — no black-box model needed.

The project moved to the full ChargePlace Scotland public archive and replaced the classifier with the Demand-Pressure Index and usage-profile clustering.

---

## Key Decisions

See `docs/model-decisions.md` for the full reasoning behind:
- Why a transparent index was chosen over a predictive classifier
- Why demand forecasting was dropped (network fragmentation evidence)
- How usage-profile clustering avoids the fragmentation problem
- Why out-of-fold predictions are used for any probability outputs

---

## Tech Stack

- Python (pandas, NumPy)
- scikit-learn (KMeans, StandardScaler, silhouette score)
- Plotly / Folium (visualisation + mapping)
- Streamlit (dashboard)
- Nominatim / geopy (geocoding)
- Poetry (dependency management)

---

## How to Run

```bash
# Install dependencies
poetry install

# Clean the raw session files (data/raw_cps/ must contain the downloaded xlsx/csv)
poetry run python src/cleaner_cps.py

# Build the charge point reference table (geocodes site names → local authority)
poetry run python src/geocode_sites.py   # slow first run (~1.1s per site); cached after
poetry run python src/build_cp_table.py

# Compute the demand-pressure index
poetry run python src/pressure_index.py

# Compute usage-profile clusters
poetry run python src/cluster_profiles.py

# Launch the dashboard
poetry run streamlit run src/dashboard.py
```

---

## Known Issues

**Geocoding coverage (~73%) — to be investigated.**

Around 27% of charge points fail to resolve to a local authority via Nominatim. I suspect the misses skew toward rural and remote sites, which could mean Highland and similar LAs are under-represented in both the pressure index and the archetype breakdown. Putting in a fix later.