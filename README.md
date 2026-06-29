# Scotland EV Charging Infrastructure Analysis

## Overview

When I discovered the UK's public EV charging network might not be keeping pace with rising EV adoption, I wanted to find out if there was enough public infrastructure to meet demand. ChargePlace Scotland publishes real session-level data, which gave me the chance to investigate. I built an end-to-end ETL pipeline on Databricks and a Streamlit dashboard to identify which sites across Scotland are facing the most infrastructure strain.

The output is a **Demand-Pressure Index** — ranks every charging site by infrastructure strain (saturation + utilisation), with a drill-down into each site's session profile.

---

## Data Source

**ChargePlace Scotland public session data** — monthly spreadsheets published at [chargeplacescotland.org](https://chargeplacescotland.org/monthly-charge-point-performance/)

**ChargePlace Scotland locations feed** — snapshots of the smart charging API (charge point metadata and coordinates)

---

## Methodology

### Pipeline (Databricks Medallion Architecture)

```
chargeplacescotland.org
    ↓ src/harvest_cps.py
Bronze: DBFS Volume (raw monthly xlsx files, immutable)
    ↓ src/harvest_locations.py
Bronze: DBFS Volume (locations snapshots)
    ↓ src/cleaner_cps.py
Silver: chargepoint_analysis.silver.cps_sessions_clean
    ↓ src/build_charge_points.py
Silver: chargepoint_analysis.silver.charge_points
    ↓ src/site_pressure.py
Gold: chargepoint_analysis.gold.site_pressure
    ↓ src/dashboard.py
Streamlit dashboard (reads from Databricks SQL)
```

### Demand-Pressure Index

Percentile-ranked composite of two signals per charge point, re-aggregated to site level in the dashboard:

- **Saturation rate** (weight 0.6) — share of time when all connectors at a charge point are simultaneously busy (queuing pressure)
- **Utilisation** (weight 0.4) — share of available connector-time that is occupied

Sites are defined as all charge points sharing the same coordinates. Rates are recomputed from summed hours (not averaged) before re-ranking across sites.

### Dashboard

The dashboard renders one interactive page:

- **Pressure map** — every site coloured on an OrRd ramp by demand-pressure score; filter by postcode area or toggle to all-Scotland view
- **Site card** — click any site to see total sessions, total energy (MWh), utilisation, and saturation rate
- **Demand over time** — monthly session count trend for the selected site
- **Session profile heatmaps** — sessions by hour × day-of-week; sessions by hour × session-length band
- **Weekday vs weekend average** — normalised daily charging profile comparing weekday and weekend behaviour

---

## Data Quality

Postcode anomaly detection runs as a separate validation step (`src/dq_postcodes.py`) using three-way triangulation:

1. **A1** — postcode area from the locations feed
2. **A2** — postcode area reverse-geocoded from coordinates (via postcodes.io)
3. **A3** — site name resolved by Claude (grounded web search) when A1 and A2 disagree

Findings are written to `chargepoint_analysis.reference.dq_findings`. Confirmed overrides are applied fix-on-read in the Silver `charge_points` table. See `docs/postcode-dq-runbook.md` for the review workflow.

---

## Key Decisions

See `docs/model-decisions.md` for the full reasoning behind:

- Why a transparent index was chosen over a predictive classifier
- Why the grain shifted from local authority to charge point (avoids ecological fallacy)
- How the saturation/utilisation weights were chosen

---

## Tech Stack

- Python (pandas, NumPy)
- Databricks (Delta Lake, Spark, Serverless compute)
- databricks-sql-connector (dashboard queries)
- Plotly / pydeck (visualisation + mapping)
- Streamlit (dashboard)
- Anthropic Claude (AI-assisted postcode data quality)
- Poetry (dependency management)

---

## How to Run

The Bronze → Gold pipeline runs on Databricks. The dashboard runs locally and connects to Databricks via SQL.

### Dashboard (local)

```bash
# Install dependencies
poetry install

# Set credentials in .env
DATABRICKS_SERVER_HOSTNAME=<your-workspace>.azuredatabricks.net
DATABRICKS_HTTP_PATH=/sql/1.0/warehouses/<warehouse-id>
DATABRICKS_TOKEN=<personal-access-token>

# Launch
poetry run streamlit run src/dashboard.py
```

### Pipeline (Databricks)

Run the scripts in order on Databricks, or attach them to a workflow:

```
1. src/harvest_cps.py          — Bronze: download monthly session files
2. src/harvest_locations.py    — Bronze: snapshot the locations feed
3. src/cleaner_cps.py          — Silver: clean and normalise sessions
4. src/build_charge_points.py  — Silver: flatten locations → per-connector table
5. src/site_pressure.py        — Gold: compute demand-pressure index
```

Set `FULL_REFRESH=1` on `cleaner_cps.py` to rebuild Silver from scratch (default is incremental).

---

## Known Issues

**Geocoding coverage — to be investigated.**

Not all cp_id have a matching id in the chargepoint locations table. Thus, there might be missing charge points in the final output map. This is also why region based analysis is removed.

**Single-connector sites conflate saturation and utilisation.**

For a site with one connector, saturation rate and utilisation are mathematically identical. These sites can rank highly on the pressure index without facing genuine queuing pressure. They are included but should be interpreted with caution.
