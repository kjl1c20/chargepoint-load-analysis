# Layer Contracts

The engineering rules for the ChargePlace Scotland Medallion pipeline. The AI reads this file before writing or changing any pipeline code. You enforce it when you verify the output.

Source data: ChargePlace Scotland monthly session files (xlsx/csv) — downloaded from [chargeplacescotland.org](https://chargeplacescotland.org/monthly-charge-point-performance/) and stored in the bronze Volume.
Catalog: `chargepoint_analysis`. One schema per layer: `bronze`, `silver`, `gold`. Every schema has a description.

---

## Naming (locked)

| Layer  | Schema                          | Object name                      | Type         |
|--------|---------------------------------|----------------------------------|--------------|
| Bronze | `chargepoint_analysis.bronze`   | `raw_files`                      | Volume       |
| Silver | `chargepoint_analysis.silver`   | `cps_sessions_clean`             | Delta table  |
| Silver | `chargepoint_analysis.silver`   | `charge_points`                  | Delta table  |
| Silver | `chargepoint_analysis.silver`   | `site_geocode_cache`             | Delta table  |
| Silver | `chargepoint_analysis.silver`   | `ocm_scotland`                   | Delta table  |
| Gold   | `chargepoint_analysis.gold`     | `demand_pressure_index`          | Delta table  |
| Gold   | `chargepoint_analysis.gold`     | `cp_clusters`                    | Delta table  |

A Silver table never lives in `bronze` or `gold`. A Gold table never lives in `bronze` or `silver`. Gold never reads from bronze.

---

## Bronze — `chargepoint_analysis.bronze`

What it is: a Unity Catalog Volume that stores the raw monthly session files exactly as published by ChargePlace Scotland. No schema enforcement. No transformation.

Volume path: `/Volumes/chargepoint_analysis/bronze/raw_files/`

Rules:
- One file per source month. File names follow the source naming convention (e.g. `APR-24-SESSIONS-CLEAN.xlsx`, `SESSIONS-CPS-APR-25.csv`).
- Files are immutable. Never overwrite or delete a file that has already been loaded. Use Delta time travel on Silver tables for corrections.
- Column names are inconsistent across file eras. Do not enforce a schema here — normalisation happens in the Silver ingestion step.
- New monthly files are either uploaded manually via the Databricks Catalog UI or harvested automatically by the `harvest` job (see Pipelines).
- Supported formats: `.xlsx` and `.csv`. Encoding fallback: UTF-8 then latin-1 for CSV files.

---

## Silver — `chargepoint_analysis.silver`

What it is: cleaned, typed, validated, and enriched data. Still detail grain — no aggregations, no KPIs.

Rules:
- Reads from the bronze Volume only. Never directly from the source website.
- All column names are normalised using `COLUMN_MAP` (defined in `src/harvest_cps.py`) before writing to Silver. This map covers all known header variants across file eras.
- **Idempotency**: at the start of each run, `silver.py` lists all files in the bronze Volume and compares against the distinct `source_file` values already present in `silver.cps_sessions_clean`. Only files not yet in Silver are processed. This makes every run safe to re-execute without duplicating data.

### `silver.cps_sessions_clean`

Grain: one row per charging session.

- Source: all files in `/Volumes/chargepoint_analysis/bronze/raw_files/`
- Partitioned by `month` (derived from `start_time`, format `YYYY-MM`)
- Deduplication key: `(cp_id, connector, start_time, consumption_kwh)` — handles sessions that appear in overlapping monthly files
- Invalid session filter (rows that fail any rule are dropped):
  - `consumption_kwh > 0` and `consumption_kwh <= 300`
  - `duration_minutes > 1` and `duration_minutes <= 1440` (24 hours)
  - `end_time > start_time`
- Date parsing: UK day-first format (`dayfirst=True`) for all timestamp columns
- Duration normalisation: accepts both `HH:MM:SS` string format and bare seconds (numeric)
- `source_file` column retained for provenance

| Column             | Type      | Notes                                      |
|--------------------|-----------|--------------------------------------------|
| `site_name`        | STRING    | Title-cased, stripped                      |
| `cp_id`            | STRING    | Stripped                                   |
| `connector_type`   | STRING    | Title-cased, stripped                      |
| `connector`        | STRING    | Stripped; forced to string (int in some files) |
| `currency`         | STRING    |                                            |
| `amount`           | DOUBLE    | Revenue paid for session                   |
| `consumption_kwh`  | DOUBLE    | Energy consumed; must be > 0 and ≤ 300     |
| `duration_minutes` | DOUBLE    | Derived from duration column               |
| `start_time`       | TIMESTAMP | UK day-first parsing                       |
| `end_time`         | TIMESTAMP | From source or derived as start + duration |
| `source_file`      | STRING    | Originating filename                       |
| `month`            | STRING    | Partition column, e.g. `2024-03`           |

### `silver.ocm_scotland`

Grain: one row per OCM point of interest (POI) in Scotland.

- Source: Open Charge Map REST API (`https://api.openchargemap.io/v3/poi/`). API key stored as Databricks secret `OCM_API_KEY`.
- Fetched as a single request centred on Scotland (lat 57.0, lon -4.0, radius 350km), filtered to bounding box lat [54.5, 61.0] lon [-9.0, -0.5].
- `ocm_connectors_per_point` = `round(total_connections / number_of_points)`, minimum 1.
- Rebuilt in full on each pipeline run (OCM data changes infrequently; a full refresh is cheap).

| Column                    | Type    | Notes                                |
|---------------------------|---------|--------------------------------------|
| `ocm_id`                  | BIGINT  | Primary key                          |
| `ocm_title`               | STRING  |                                      |
| `ocm_lat`                 | DOUBLE  |                                      |
| `ocm_lon`                 | DOUBLE  |                                      |
| `ocm_n_connections`       | BIGINT  | Total connection count from OCM      |
| `ocm_n_points`            | BIGINT  |                                      |
| `ocm_connectors_per_point`| BIGINT  | Used as the OCM connector count      |

### `silver.charge_points`

Grain: one row per `cp_id`.

- Source: `silver.cps_sessions_clean` joined with `silver.ocm_scotland`.
- Run order: `cps_sessions_clean` must complete before `charge_points` starts within the same `silver.py` run.
- `site_name`: mode (most frequent) across all sessions for that `cp_id`
- `connector_type`: mode across all sessions
- `n_connectors`: `max(ocm_connectors_per_point, session-observed distinct connector IDs)`. Match CPS charge points to OCM POIs via nearest-neighbour spatial join (KDTree on 3D unit-sphere coordinates), threshold 200m. If no OCM match within 200m, use session-observed count only.
- Geocoding: `site_name` resolved to `latitude`, `longitude`, `local_authority`, `postcode` via the geocode cache. Unresolved sites leave those columns NULL and are tracked separately.
- Rebuilt in full on each pipeline run.

| Column             | Type    | Notes                                        |
|--------------------|---------|----------------------------------------------|
| `cp_id`            | STRING  | Primary key                                  |
| `site_name`        | STRING  | Modal site name from sessions                |
| `connector_type`   | STRING  | Modal connector type from sessions           |
| `n_connectors`     | BIGINT  | max(OCM, observed)                           |
| `latitude`         | DOUBLE  | NULL if unresolved                           |
| `longitude`        | DOUBLE  | NULL if unresolved                           |
| `local_authority`  | STRING  | NULL if unresolved (~27% of charge points)   |
| `postcode`         | STRING  | NULL if unresolved                           |
| `geocode_method`   | STRING  | e.g. `nominatim`, `manual`                   |

### `silver.site_geocode_cache`

Grain: one row per `site_name`.

- Persists Nominatim geocoding results across pipeline runs
- Write pattern: `MERGE INTO` on `site_name` — never re-geocode a site that already has a result
- Nominatim rate limit: 1 request/second. Respect it; never bypass the cache.
- Sites that fail geocoding are written with NULL coordinates and `geocode_method = 'failed'`

| Column             | Type      | Notes                             |
|--------------------|-----------|-----------------------------------|
| `site_name`        | STRING    | Primary key (merge key)           |
| `latitude`         | DOUBLE    | NULL if failed                    |
| `longitude`        | DOUBLE    | NULL if failed                    |
| `local_authority`  | STRING    | NULL if failed                    |
| `postcode`         | STRING    | NULL if failed                    |
| `geocode_method`   | STRING    | `nominatim` or `failed`           |
| `geocoded_at`      | TIMESTAMP | Timestamp of geocoding attempt    |

---

## Gold — `chargepoint_analysis.gold`

What it is: business-ready tables, one per analytical output, shaped for the Streamlit dashboard.

Rules:
- Reads from `chargepoint_analysis.silver` only. Never from the bronze Volume, never from raw files. Never a mix of layers.
- One table per analytical output. Each table's grain is fixed and documented in its comment.
- Rebuilt in full on each pipeline run.
- **Saturation and utilisation are never mixed with revenue.** Revenue (`amount`) is reported as a separate commercial lens only — never folded into the pressure score.
- **Pressure score weighting is locked**: `saturation_rate × 0.6 + utilisation × 0.4`. Any change to weights must be documented in `docs/model-decisions.md` before the pipeline code is updated.

### `gold.demand_pressure_index`

Grain: one row per local authority.
Question it answers: which Scottish local authorities are under the most EV charging infrastructure pressure?

- Excludes charge points where `local_authority IS NULL`
- `utilisation` = `occupied_hours / available_connector_hours`, clipped to 1.0. If `available_connector_hours = 0`, set `utilisation = 0`.
- `saturation_rate` = `saturated_hours / cp_available_hours`. If `cp_available_hours = 0`, set `saturation_rate = 0`.
- Saturation uses a sweep-line concurrency algorithm per charge point, threshold `k = n_connectors`
- Available hours calculated as `last_seen - first_seen` per connector (not a fixed window) to avoid counting dead time for decommissioned connectors
- `pressure_score` = weighted average of percentile ranks: `saturation_rate_pct × 0.6 + utilisation_pct × 0.4`

| Column                     | Type    | Notes                                              |
|----------------------------|---------|----------------------------------------------------|
| `local_authority`          | STRING  | Primary key                                        |
| `n_chargepoints`           | BIGINT  |                                                    |
| `n_connectors`             | BIGINT  |                                                    |
| `total_sessions`           | BIGINT  |                                                    |
| `total_energy_kwh`         | DOUBLE  |                                                    |
| `total_revenue`            | DOUBLE  | Reported separately; not in pressure score         |
| `occupied_hours`           | DOUBLE  |                                                    |
| `available_connector_hours`| DOUBLE  |                                                    |
| `saturated_hours`          | DOUBLE  |                                                    |
| `cp_available_hours`       | DOUBLE  |                                                    |
| `latitude`                 | DOUBLE  | Mean latitude of charge points in LA               |
| `longitude`                | DOUBLE  | Mean longitude of charge points in LA              |
| `utilisation`              | DOUBLE  | In [0, 1]                                          |
| `saturation_rate`          | DOUBLE  | In [0, 1]                                          |
| `revenue_per_connector`    | DOUBLE  |                                                    |
| `saturation_rate_pct`      | DOUBLE  | Percentile rank of saturation_rate                 |
| `utilisation_pct`          | DOUBLE  | Percentile rank of utilisation                     |
| `pressure_score`           | DOUBLE  | Weighted percentile score in [0, 1]                |
| `pressure_rank`            | BIGINT  | 1 = highest pressure                               |

### `gold.cp_clusters`

Grain: one row per charge point (minimum 50 sessions).
Question it answers: what are the behavioural archetypes of Scottish charge points?

- Charge points with fewer than 50 sessions are excluded
- Features are StandardScaler-normalised before clustering
- `k` chosen by silhouette score over range 3–7; log silhouette scores to MLflow for each run
- Archetype labels are rule-based on cluster centroids (not re-trained per run). Rules are evaluated in priority order — first match wins:
  1. `rapid_share >= 0.5` → `Rapid top-up`
  2. `median_duration_min >= 600` → `AC depot / long-stay`
  3. `pct_morning >= 0.35` AND `weekend_ratio < 0.15` → `AC commuter`
  4. Otherwise → `AC public / retail`

| Column                | Type    | Notes                                              |
|-----------------------|---------|----------------------------------------------------|
| `cp_id`               | STRING  | Primary key                                        |
| `n_sessions`          | BIGINT  | Must be >= 50                                      |
| `pct_morning`         | DOUBLE  | Share of sessions starting 06:00–09:59             |
| `pct_midday`          | DOUBLE  | Share of sessions starting 10:00–15:59             |
| `pct_evening`         | DOUBLE  | Share of sessions starting 16:00–21:59             |
| `pct_overnight`       | DOUBLE  | Share of sessions starting 22:00–05:59             |
| `weekend_ratio`       | DOUBLE  | Share of sessions on Saturday or Sunday            |
| `rapid_share`         | DOUBLE  | Share of sessions on a rapid connector             |
| `median_duration_min` | DOUBLE  |                                                    |
| `median_energy_kwh`   | DOUBLE  |                                                    |
| `cluster`             | INT     | KMeans cluster ID                                  |
| `archetype`           | STRING  | Human-readable archetype label                     |
| `local_authority`     | STRING  | From `silver.charge_points`; NULL if unresolved    |

---

## Data Quality Rules

| Table                        | Check                                         | Action on failure |
|------------------------------|-----------------------------------------------|-------------------|
| `silver.cps_sessions_clean`  | `consumption_kwh > 0`                         | Drop row          |
| `silver.cps_sessions_clean`  | `consumption_kwh <= 300`                      | Drop row          |
| `silver.cps_sessions_clean`  | `duration_minutes > 1`                        | Drop row          |
| `silver.cps_sessions_clean`  | `duration_minutes <= 1440`                    | Drop row          |
| `silver.cps_sessions_clean`  | `end_time > start_time`                       | Drop row          |
| `silver.charge_points`       | `n_connectors >= 1`                           | Alert             |
| `silver.cps_sessions_clean`  | `start_time IS NOT NULL`                      | Drop row          |
| `silver.cps_sessions_clean`  | `cp_id IS NOT NULL`                           | Drop row          |
| `gold.demand_pressure_index` | `utilisation` in [0, 1]                       | Fail pipeline     |
| `gold.demand_pressure_index` | `saturation_rate` in [0, 1]                   | Fail pipeline     |
| `gold.demand_pressure_index` | Row count = distinct local authorities in Silver | Alert           |
| `gold.cp_clusters`           | `n_sessions >= 50` for all rows               | Fail pipeline     |

Geocoding coverage must be ≥ 73% (current baseline). Alert if it drops below this threshold after any pipeline run.

---

## Pipelines and the job

- One notebook per layer: `databricks/harvest.py`, `databricks/silver.py`, `databricks/gold.py`
- One Databricks Workflow job that runs notebooks in order: Harvest → Silver → Gold. Each stage must complete successfully before the next starts.
- The job is scheduled monthly to align with ChargePlace Scotland's monthly data publication cadence.
- Serverless compute.

| Notebook               | Reads from                        | Writes to                                                          |
|------------------------|-----------------------------------|--------------------------------------------------------------------|
| `databricks/harvest.py`| CPS website (WordPress media API) | `bronze.raw_files` Volume                                          |
| `databricks/silver.py` | `bronze.raw_files` Volume, OCM API | `silver.cps_sessions_clean`, `silver.ocm_scotland`, `silver.charge_points`, `silver.site_geocode_cache` |
| `databricks/gold.py`   | `silver.*`                        | `gold.demand_pressure_index`, `gold.cp_clusters`                   |
