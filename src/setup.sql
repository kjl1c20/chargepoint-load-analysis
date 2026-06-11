-- One-time environment setup: run this in a Databricks SQL warehouse or notebook
-- before executing any pipeline job for the first time in any environment.
-- Order matters: catalog → schema → volume/table.

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
    cp_id               STRING    NOT NULL    COMMENT 'Charge point identifier — matches cp_id in cps_sessions_clean',
    site_name           STRING                COMMENT 'Site name from CPS feed',
    address             STRING                COMMENT 'Street address',
    city                STRING                COMMENT 'City',
    postcode            STRING                COMMENT 'Postcode',
    latitude            DOUBLE                COMMENT 'WGS84 latitude',
    longitude           DOUBLE                COMMENT 'WGS84 longitude',
    connector_type      STRING                COMMENT 'AC or DC',
    max_charge_rate_kw  DOUBLE                COMMENT 'Maximum charge rate in kW',
    network_status      STRING                COMMENT 'AVAILABLE / CHARGING / INOPERATIVE / UNKNOWN',
    ingested_at         TIMESTAMP NOT NULL    COMMENT 'Pipeline ingest timestamp (UTC)',
    CONSTRAINT charge_points_pk PRIMARY KEY (cp_id)
)
USING DELTA
COMMENT 'Active CPS charge points with coordinates — Silver layer. Built by build_charge_points.py from Bronze locations feed.';
