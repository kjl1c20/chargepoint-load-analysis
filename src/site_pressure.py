"""Build the Gold Demand-Pressure Index from the two Silver Delta tables (Spark job).

Ranks individual charge points (cp_id) by saturation + utilisation so the output answers
*which site* to expand. Runs on Databricks compute (needs an active SparkSession), like
build_charge_points.py. Writes chargepoint_analysis.gold.site_pressure.

  - saturation k = n_connectors from Silver charge_points (no OCM, no session-observed fallback)
  - charge points with no location match are dropped (can't place / no k)
  - geography is postcode-based (local_authority dropped); postcode_area drives regional filtering
"""

import os
import logging

from pyspark.sql import SparkSession, Window
from pyspark.sql import functions as F

from features import compute_saturation, SAT_SCHEMA, MIN_SESSIONS_SITE, PRESSURE_WEIGHTS

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

spark = SparkSession.getActiveSession() or SparkSession.builder.getOrCreate()

SESSIONS_TABLE = os.getenv("SILVER_SESSIONS_TABLE", "chargepoint_analysis.silver.cps_sessions_clean")
CP_TABLE = os.getenv("SILVER_CP_TABLE", "chargepoint_analysis.silver.charge_points")
GOLD_TABLE = os.getenv("GOLD_SITE_PRESSURE_TABLE", "chargepoint_analysis.gold.site_pressure")

GOLD_COLUMNS = [
    "cp_id", "pressure_rank", "pressure_score", "saturation_rate", "utilisation",
    "saturated_hours", "cp_available_hours", "occupied_hours", "available_connector_hours",
    "total_sessions", "total_energy_kwh", "total_revenue", "revenue_per_connector",
    "n_connectors", "single_connector", "site_name", "postcode", "postcode_area",
    "latitude", "longitude", "ingested_at",
]


def main():
    sessions = spark.table(SESSIONS_TABLE)
    cps = spark.table(CP_TABLE)

    # cp-level reference (one row per cp_id) from the per-connector Silver table.
    # n_connectors / site_name / postcode / coords are identical across an EVSE's rows.
    cp_ref = cps.groupBy("cp_id").agg(
        F.first("n_connectors", ignorenulls=True).alias("n_connectors"),
        F.first("site_name", ignorenulls=True).alias("site_name"),
        F.first("postcode", ignorenulls=True).alias("postcode"),
        F.first("latitude", ignorenulls=True).alias("latitude"),
        F.first("longitude", ignorenulls=True).alias("longitude"),
    )

    # Join only the saturation k (n_connectors) onto sessions — not the full cp_ref, whose
    # site_name would collide with the sessions' own site_name column. Geography is joined
    # later, once, at cp grain. Inner join drops sessions with no location match (no k).
    cp_k = cp_ref.select("cp_id", "n_connectors")
    s = (
        sessions.join(F.broadcast(cp_k), "cp_id", "inner")
        # end_time is nullable in Silver; fall back to start + duration for windows/sweep-line.
        .withColumn(
            "end_time_filled",
            F.coalesce(
                F.col("end_time"),
                (F.col("start_time").cast("long") + F.col("duration_minutes") * 60).cast("timestamp"),
            ),
        )
        .withColumn("occupied_hours", F.col("duration_minutes") / 60.0)
    )

    # ---- per-connector availability windows → cp-level available connector hours ----
    conn = (
        s.groupBy("cp_id", "connector")
        .agg(F.min("start_time").alias("first_seen"), F.max("end_time_filled").alias("last_seen"))
        .withColumn(
            "available_hours",
            (F.col("last_seen").cast("long") - F.col("first_seen").cast("long")) / 3600.0,
        )
    )
    cp_avail = conn.groupBy("cp_id").agg(
        F.sum("available_hours").alias("available_connector_hours"),
    )

    # ---- cp-level totals ----
    cp_core = s.groupBy("cp_id").agg(
        F.count(F.lit(1)).cast("long").alias("total_sessions"),
        F.sum("occupied_hours").alias("occupied_hours"),
        F.sum("consumption_kwh").alias("total_energy_kwh"),
        F.sum("amount").alias("total_revenue"),
    )

    # ---- saturation (sweep-line per cp_id, k = n_connectors) via applyInPandas ----
    sat = (
        s.select(
            "cp_id",
            "start_time",
            F.col("end_time_filled").alias("end_time"),
            "n_connectors",
        )
        .groupBy("cp_id")
        .applyInPandas(compute_saturation, schema=SAT_SCHEMA)
    )

    # ---- assemble per-cp metrics ----
    # Guard every computed denominator with when/otherwise: short-circuits so the division
    # is never evaluated for a zero denominator (ANSI mode would otherwise abort the job).
    cp = (
        cp_core.join(cp_avail, "cp_id")
        .join(sat, "cp_id")
        .join(cp_ref, "cp_id")
        .withColumn(
            "utilisation",
            F.when(F.col("available_connector_hours") == 0, F.lit(None))
            .otherwise(F.least(F.col("occupied_hours") / F.col("available_connector_hours"), F.lit(1.0))),
        )
        .withColumn(
            "saturation_rate",
            F.when(F.col("cp_available_hours") == 0, F.lit(None))
            .otherwise(F.col("saturated_hours") / F.col("cp_available_hours")),
        )
        .withColumn(
            "revenue_per_connector",
            F.when(F.col("n_connectors") == 0, F.lit(None))
            .otherwise(F.col("total_revenue") / F.col("n_connectors")),
        )
        .withColumn("single_connector", F.col("n_connectors") == 1)
    )

    before = cp.count()
    cp = cp.filter(F.col("total_sessions") >= MIN_SESSIONS_SITE)
    cp = cp.filter(F.col("latitude").isNotNull() & F.col("longitude").isNotNull())
    logger.info("Charge points: %d total → %d after session floor (%d) + geocode filter",
                before, cp.count(), MIN_SESSIONS_SITE)

    # ---- weighted percentile-rank pressure index (grain = cp_id) ----
    w_sat = PRESSURE_WEIGHTS["saturation_rate"]
    w_util = PRESSURE_WEIGHTS["utilisation"]
    cp = (
        cp.withColumn("saturation_rate_pct", F.cume_dist().over(Window.orderBy("saturation_rate")))
        .withColumn("utilisation_pct", F.cume_dist().over(Window.orderBy("utilisation")))
        .withColumn(
            "pressure_score",
            (w_sat * F.col("saturation_rate_pct") + w_util * F.col("utilisation_pct")) / (w_sat + w_util),
        )
        .withColumn("pressure_rank", F.rank().over(Window.orderBy(F.col("pressure_score").desc())))
    )

    # ---- postcode area (G, EH, AB ...) for regional filtering ----
    cp = cp.withColumn(
        "postcode_area",
        F.regexp_extract(F.upper(F.trim(F.col("postcode"))), r"^([A-Z]{1,2})", 1),
    ).withColumn(
        "postcode_area",
        F.when(F.col("postcode_area") == "", F.lit(None)).otherwise(F.col("postcode_area")),
    )

    cp = cp.withColumn("ingested_at", F.current_timestamp())

    out = cp.select(*GOLD_COLUMNS)
    out.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(GOLD_TABLE)
    logger.info("Written %d charge points to %s", out.count(), GOLD_TABLE)


if __name__ == "__main__":
    main()
