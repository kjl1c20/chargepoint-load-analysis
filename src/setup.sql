-- ============================================================
-- CATALOG
-- ============================================================
CREATE CATALOG IF NOT EXISTS chargepoint_analysis;

-- ============================================================
-- BRONZE
-- ============================================================
CREATE SCHEMA IF NOT EXISTS chargepoint_analysis.bronze;

-- Managed volume: Databricks controls the storage location.
-- Raw CPS monthly files (xlsx/csv) are written here by harvest_cps.py.
-- Path resolves to: /Volumes/chargepoint_analysis/bronze/raw_cps
CREATE VOLUME IF NOT EXISTS chargepoint_analysis.bronze.raw_cps;

-- Weekly snapshots of the CPS locations feed (charge points, coordinates, connector specs).
-- Written by harvest_locations.py as dated JSON files: locations_YYYY-MM-DD.json
-- Path resolves to: /Volumes/chargepoint_analysis/bronze/locations
CREATE VOLUME IF NOT EXISTS chargepoint_analysis.bronze.locations;

-- ============================================================
-- SILVER
-- ============================================================
CREATE SCHEMA IF NOT EXISTS chargepoint_analysis.silver;

CREATE TABLE IF NOT EXISTS chargepoint_analysis.silver.cps_sessions_clean (
    site_name        STRING                COMMENT 'Human-readable site name',
    cp_id            STRING    NOT NULL    COMMENT 'Charge point identifier',
    connector_type   STRING                COMMENT 'AC / DC / Rapid etc.',
    connector        STRING    NOT NULL    COMMENT 'Connector number within the charge point',
    currency         STRING                COMMENT 'ISO 4217 currency code',
    amount           DOUBLE                COMMENT 'Amount paid by the driver',
    consumption_kwh  DOUBLE    NOT NULL    COMMENT 'Energy delivered in kWh',
    duration_minutes DOUBLE    NOT NULL    COMMENT 'Session duration in minutes',
    start_time       TIMESTAMP NOT NULL    COMMENT 'Session start (UTC)',
    end_time         TIMESTAMP             COMMENT 'Session end (UTC)',
    source_file      STRING    NOT NULL    COMMENT 'Source Bronze filename',
    ingested_at      TIMESTAMP NOT NULL    COMMENT 'Pipeline ingest timestamp (UTC)',
    year_month       STRING    NOT NULL    COMMENT 'Partition key YYYY-MM',
    CONSTRAINT cps_sessions_pk PRIMARY KEY (cp_id, connector, start_time)
)
USING DELTA
PARTITIONED BY (year_month)
COMMENT 'Cleaned CPS charging sessions — Silver layer';

CREATE TABLE IF NOT EXISTS chargepoint_analysis.silver.charge_points (
    cp_id               STRING    NOT NULL    COMMENT 'Charge point (EVSE) identifier — matches cp_id in cps_sessions_clean',
    connector_id        STRING    NOT NULL    COMMENT 'Connector id within the EVSE — matches connector in cps_sessions_clean',
    n_connectors        INT       NOT NULL    COMMENT 'Number of connectors on this EVSE (same for every row sharing a cp_id)',
    site_name           STRING                COMMENT 'Site name from CPS feed',
    address             STRING                COMMENT 'Street address',
    city                STRING                COMMENT 'City',
    postcode            STRING                COMMENT 'Postcode (feed value, or approved override)',
    postcode_source     STRING                COMMENT 'feed | override — lineage of the postcode value',
    latitude            DOUBLE                COMMENT 'WGS84 latitude',
    longitude           DOUBLE                COMMENT 'WGS84 longitude',
    connector_type      STRING                COMMENT 'AC or DC — per connector',
    max_charge_rate_kw  DOUBLE                COMMENT 'Maximum charge rate in kW — per connector',
    network_status      STRING                COMMENT 'EVSE status: AVAILABLE / CHARGING / INOPERATIVE / UNKNOWN',
    source_snapshot     STRING    NOT NULL    COMMENT 'Bronze locations filename this row was built from',
    ingested_at         TIMESTAMP NOT NULL    COMMENT 'Pipeline ingest timestamp (UTC)',
    CONSTRAINT charge_points_pk PRIMARY KEY (cp_id, connector_id)
)
USING DELTA
COMMENT 'CPS connectors with coordinates — Silver layer, one row per physical connector. Built by build_charge_points.py from Bronze locations feed.';

-- ============================================================
-- GOLD
-- ============================================================
CREATE SCHEMA IF NOT EXISTS chargepoint_analysis.gold;

-- Demand-Pressure Index, one row per charge point (cp_id). Built by site_pressure.py
-- (Spark) from the two Silver tables. Ranks where to expand existing strained sites.
CREATE TABLE IF NOT EXISTS chargepoint_analysis.gold.site_pressure (
    cp_id                     STRING    NOT NULL  COMMENT 'Charge point (EVSE) identifier',
    pressure_rank             INT                 COMMENT 'Rank by pressure_score (1 = most pressured)',
    pressure_score            DOUBLE              COMMENT '0–1 weighted percentile of saturation (0.6) + utilisation (0.4)',
    saturation_rate           DOUBLE              COMMENT 'Share of cp time all connectors simultaneously busy',
    utilisation               DOUBLE              COMMENT 'Occupied connector-hours / available connector-hours (≤1)',
    saturated_hours           DOUBLE              COMMENT 'Hours with >= n_connectors concurrent sessions',
    cp_available_hours        DOUBLE              COMMENT 'First-seen → last-seen window for the charge point',
    occupied_hours            DOUBLE              COMMENT 'Total connector-hours occupied',
    available_connector_hours DOUBLE              COMMENT 'Sum of per-connector availability windows',
    total_sessions            BIGINT              COMMENT 'Session count (>= MIN_SESSIONS_SITE floor)',
    total_energy_kwh          DOUBLE              COMMENT 'Total energy delivered (kWh)',
    total_revenue             DOUBLE              COMMENT 'Total amount paid (commercial lens, not in pressure)',
    revenue_per_connector     DOUBLE              COMMENT 'total_revenue / n_connectors',
    n_connectors              INT                 COMMENT 'Connectors on this charge point (from Silver charge_points)',
    single_connector          BOOLEAN             COMMENT 'n_connectors == 1 (saturation == utilisation by construction)',
    site_name                 STRING              COMMENT '',
    postcode                  STRING              COMMENT '',
    postcode_area             STRING              COMMENT 'Leading letters of postcode (G, EH, AB ...) for regional filtering',
    latitude                  DOUBLE              COMMENT '',
    longitude                 DOUBLE              COMMENT 'WGS84 longitude',
    ingested_at               TIMESTAMP NOT NULL  COMMENT 'Pipeline run timestamp (UTC)',
    CONSTRAINT site_pressure_pk PRIMARY KEY (cp_id)
)
USING DELTA
COMMENT 'Demand-Pressure Index per charge point — Gold layer. Built by site_pressure.py from Silver.';

-- ============================================================
-- REFERENCE (data-quality)
-- ============================================================
CREATE SCHEMA IF NOT EXISTS chargepoint_analysis.reference;

-- Allowlist of valid Scottish postcode areas. Any charge point whose postcode area is not
-- in this set is, for a Scotland-only network, an anomaly by definition.
CREATE TABLE IF NOT EXISTS chargepoint_analysis.reference.postcode_areas (
    area_code  STRING  NOT NULL  COMMENT 'Postcode area (leading letters)',
    area_name  STRING  NOT NULL  COMMENT 'Place name for the area',
    CONSTRAINT postcode_areas_pk PRIMARY KEY (area_code)
)
USING DELTA
COMMENT 'Valid Scottish postcode areas — reference allowlist for postcode validation.';

-- Idempotent seed of the 16 Scottish postcode areas. INSERT INTO WILL KEEP ADDING NEW REPEATED AREAS TO THE TABLE IF RERUN.
MERGE INTO chargepoint_analysis.reference.postcode_areas AS t
USING (
    SELECT * FROM VALUES
        ('AB', 'Aberdeen'),        ('DD', 'Dundee'),         ('DG', 'Dumfries'),
        ('EH', 'Edinburgh'),       ('FK', 'Falkirk'),        ('G',  'Glasgow'),
        ('HS', 'Outer Hebrides'),  ('IV', 'Inverness'),      ('KA', 'Kilmarnock'),
        ('KW', 'Kirkwall'),        ('KY', 'Kirkcaldy'),      ('ML', 'Motherwell'),
        ('PA', 'Paisley'),         ('PH', 'Perth'),          ('TD', 'Borders'),
        ('ZE', 'Shetland')
    AS v(area_code, area_name)
) AS s
ON t.area_code = s.area_code
WHEN NOT MATCHED THEN INSERT (area_code, area_name) VALUES (s.area_code, s.area_name);

-- NOTE: postcode corrections are NOT a table. They are a curated in-code mapping
-- (POSTCODE_OVERRIDES in build_charge_points.py), applied fix-on-read when building Silver.
-- At ≤ a few hand-approved entries, a versioned dict (git history = audit trail) is more
-- proportionate than a Delta table. Bronze stays immutable either way.

-- Generic data-quality findings register. Every pipeline check writes here (one producer
-- today: postcode triangulation). Fixed columns stay thin; check-specific payload lives in
-- the `details` JSON, so new checks reuse the table without schema changes.
CREATE TABLE IF NOT EXISTS chargepoint_analysis.reference.dq_findings (
    check_name   STRING    NOT NULL  COMMENT 'Check id, encodes table+rule e.g. charge_points.postcode_triangulation',
    entity_id    STRING    NOT NULL  COMMENT 'Key of the offending row (cp_id, or composite key as text)',
    message      STRING              COMMENT 'Human-readable: what is wrong (carries the bad value + rule)',
    details      STRING              COMMENT 'JSON: check-specific payload (coord_postcode, suggested_postcode/address, ...)',
    status       STRING              COMMENT 'open | resolved | dismissed',
    detected_at  TIMESTAMP           COMMENT 'When first flagged (UTC)',
    resolved_at  TIMESTAMP           COMMENT 'When closed (UTC), null while open',
    CONSTRAINT dq_findings_pk PRIMARY KEY (check_name, entity_id)
)
USING DELTA
COMMENT 'Pipeline data-quality findings register. Written by dq_postcodes.py (and future checks).';
