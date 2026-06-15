"""Postcode data-quality: flag anomalies by triangulation, then suggest fixes with Sonnet.

Writes to the generic data-quality register `reference.dq_findings` — fixed columns stay
thin (check_name, entity_id, message, status, timestamps), and everything postcode-specific
(verdict, coord_postcode, suggested fix) lives in the `details` JSON. New checks reuse the
same table by writing a different `check_name` + `details`.

One Databricks job, two ordered phases:

  flag_anomalies()  — deterministic. Reverse-geocode each charge point's coordinates
                      (postcodes.io), compare the feed postcode area (A1) against the
                      coordinate-derived area (A2) and the Scottish allowlist, and upsert
                      findings into reference.dq_findings (status='open'). Committed first.
                      Findings no longer flagged are auto-resolved.

  suggest_fixes()   — async AI (skippable via --skip-ai / DQ_SKIP_AI). For each open finding
                      without a suggestion yet, a grounded Claude Sonnet batch proposes a fix
                      and merges {issue, suggested_postcode, suggested_address, description}
                      into the finding's `details`.

Neither phase touches Bronze or Silver. A human reviews findings, applies an approved
correction to reference.postcode_overrides, and build_charge_points.py picks it up.

Run:  poetry run python src/dq_postcodes.py [--skip-ai]   (on Databricks)
Needs: ANTHROPIC_API_KEY (AI phase only); internet egress to postcodes.io + Anthropic.
"""

import os
import re
import json
import time
import logging
import argparse

import requests

from pyspark.sql import SparkSession, functions as F

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

spark = SparkSession.getActiveSession() or SparkSession.builder.getOrCreate()

SILVER_TABLE = os.getenv("SILVER_CP_TABLE", "chargepoint_analysis.silver.charge_points")
AREAS_TABLE = os.getenv("POSTCODE_AREAS_TABLE", "chargepoint_analysis.reference.postcode_areas")
DQ_FINDINGS_TABLE = os.getenv("DQ_FINDINGS_TABLE", "chargepoint_analysis.reference.dq_findings")

CHECK_NAME = "charge_points.postcode_triangulation"
VERDICT_MSG = {
    "postcode_not_scottish": "not a Scottish postcode area",
    "area_mismatch": "postcode area disagrees with the coordinates",
    "postcode_missing": "no postcode on record",
}

POSTCODES_IO_BULK = "https://api.postcodes.io/postcodes"
AREA_RE = re.compile(r"^([A-Z]{1,2})")

AI_MODEL = os.getenv("DQ_AI_MODEL", "claude-sonnet-4-6")
BATCH_POLL_SECONDS = int(os.getenv("DQ_BATCH_POLL_SECONDS", "30"))
BATCH_MAX_WAIT_SECONDS = int(os.getenv("DQ_BATCH_MAX_WAIT_SECONDS", "3600"))

FINDING_SCHEMA = "check_name string, entity_id string, message string, details string"


# ============================================================
# helpers
# ============================================================

def _area(postcode) -> str | None:
    """Postcode area = leading 1–2 letters (G, EH, ML ...). None if unparseable."""
    if not postcode:
        return None
    m = AREA_RE.match(str(postcode).strip().upper())
    return m.group(1) if m else None


def _reverse_geocode(points: list[tuple[float, float]]) -> list[str | None]:
    """Bulk reverse-geocode (lat, lon) → nearest postcode via postcodes.io, order preserved."""
    out: list[str | None] = []
    for i in range(0, len(points), 100):
        chunk = points[i:i + 100]
        body = {"geolocations": [
            {"longitude": lon, "latitude": lat, "limit": 1, "radius": 2000}
            for (lat, lon) in chunk
        ]}
        try:
            resp = requests.post(POSTCODES_IO_BULK, json=body, timeout=30)
            resp.raise_for_status()
            for entry in resp.json().get("result", []):
                res = entry.get("result")
                out.append(res[0]["postcode"] if res else None)
        except requests.RequestException as e:
            logger.warning("postcodes.io chunk failed (%s) — coords unresolved for %d points", e, len(chunk))
            out.extend([None] * len(chunk))
    return out


def _verdict(feed_area, coord_area, allowlist) -> str | None:
    """Triangulation verdict. None = no anomaly (don't flag)."""
    if not feed_area:
        return "postcode_missing"
    if feed_area not in allowlist:
        return "postcode_not_scottish"
    if coord_area and coord_area in allowlist and coord_area != feed_area:
        return "area_mismatch"
    return None  # feed area is a valid Scottish area and agrees with coords (or coords unknown)


def _extract_json(message) -> dict | None:
    text = "".join(b.text for b in message.content if getattr(b, "type", None) == "text")
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


# ============================================================
# phase 1 — deterministic flagging → dq_findings
# ============================================================

def flag_anomalies():
    cps = (
        spark.table(SILVER_TABLE)
        .groupBy("cp_id")
        .agg(F.first("site_name", ignorenulls=True).alias("site_name"),
             F.first("postcode", ignorenulls=True).alias("postcode"),
             F.first("latitude", ignorenulls=True).alias("latitude"),
             F.first("longitude", ignorenulls=True).alias("longitude"))
        .toPandas()
    )
    logger.info("Checking %d charge points", len(cps))

    allowlist = {r["area_code"] for r in spark.table(AREAS_TABLE).select("area_code").toPandas().to_dict("records")}

    has_coords = cps["latitude"].notna() & cps["longitude"].notna()
    pts = [(float(la), float(lo)) for la, lo in zip(cps.loc[has_coords, "latitude"], cps.loc[has_coords, "longitude"])]
    cps["coord_postcode"] = None
    cps.loc[has_coords, "coord_postcode"] = _reverse_geocode(pts)

    cps["feed_area"] = cps["postcode"].map(_area)
    cps["coord_area"] = cps["coord_postcode"].map(_area)
    cps["verdict"] = [_verdict(fa, ca, allowlist) for fa, ca in zip(cps["feed_area"], cps["coord_area"])]

    flagged = cps[cps["verdict"].notna()]
    logger.info("Flagged %d / %d charge points: %s",
                len(flagged), len(cps), flagged["verdict"].value_counts().to_dict())

    # Map each flagged row to a generic finding (postcode specifics → details JSON).
    findings = [{
        "check_name": CHECK_NAME,
        "entity_id": r["cp_id"],
        "message": f"Feed postcode {r['postcode']!r} (area {r['feed_area']!r}) — "
                   f"{VERDICT_MSG.get(r['verdict'], r['verdict'])}.",
        "details": json.dumps({
            "verdict": r["verdict"], "site_name": r["site_name"],
            "feed_postcode": r["postcode"], "feed_area": r["feed_area"],
            "coord_postcode": r["coord_postcode"], "coord_area": r["coord_area"],
        }),
    } for r in flagged.to_dict("records")]

    # Empty list still creates a typed (empty) view so disappeared findings get auto-resolved.
    spark.createDataFrame(findings, schema=FINDING_SCHEMA).createOrReplaceTempView("dq_flagged")
    spark.sql(f"""
        MERGE INTO {DQ_FINDINGS_TABLE} t
        USING dq_flagged s ON t.check_name = s.check_name AND t.entity_id = s.entity_id
        WHEN MATCHED AND t.status = 'resolved' THEN UPDATE SET
            t.status = 'open', t.resolved_at = NULL, t.message = s.message, t.details = s.details
        WHEN NOT MATCHED THEN INSERT
            (check_name, entity_id, message, details, status, detected_at)
            VALUES (s.check_name, s.entity_id, s.message, s.details, 'open', current_timestamp())
        WHEN NOT MATCHED BY SOURCE AND t.check_name = '{CHECK_NAME}' AND t.status = 'open'
            THEN UPDATE SET t.status = 'resolved', t.resolved_at = current_timestamp()
    """)
    # Open findings that are still flagged are intentionally left untouched — that preserves
    # any AI suggestion already merged into their details.
    logger.info("dq_findings upserted (deterministic flags committed)")


# ============================================================
# phase 2 — AI fix suggestions (Sonnet, grounded, batch) → details
# ============================================================

SYSTEM = (
    "You verify UK postcodes for ChargePlace Scotland EV charge sites. Every site is in "
    "Scotland (postcode areas: AB DD DG EH FK G HS IV KA KW KY ML PA PH TD ZE). You are given "
    "a site name, the postcode currently on record, a postcode reverse-geocoded from the "
    "site's coordinates, and the coordinates. BOTH the postcode and the coordinates may be "
    "wrong. Use web search/fetch to find where the named site actually is, then decide the "
    "correct postcode and address. Reply with ONLY a JSON object and nothing else:\n"
    '{"issue": "<what is wrong, one phrase>", "suggested_postcode": "<correct postcode>", '
    '"suggested_address": "<full address>", "description": "<one concise sentence>"}'
)


def suggest_fixes():
    import anthropic
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    pending = spark.sql(f"""
        SELECT f.entity_id AS cp_id, f.details AS details, cp.latitude, cp.longitude
        FROM {DQ_FINDINGS_TABLE} f
        LEFT JOIN (SELECT cp_id, first(latitude) AS latitude, first(longitude) AS longitude
                   FROM {SILVER_TABLE} GROUP BY cp_id) cp ON f.entity_id = cp.cp_id
        WHERE f.check_name = '{CHECK_NAME}' AND f.status = 'open'
          AND get_json_object(f.details, '$.suggested_postcode') IS NULL
    """).toPandas()

    if pending.empty:
        logger.info("No open findings need an AI suggestion")
        return

    details_by_cp = {str(r.cp_id): json.loads(r.details) for r in pending.itertuples()}

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    batch_requests = []
    for r in pending.itertuples():
        d = details_by_cp[str(r.cp_id)]
        user = (f"Site name: {d.get('site_name')}\nPostcode on record: {d.get('feed_postcode')}\n"
                f"Postcode from coordinates: {d.get('coord_postcode')}\n"
                f"Coordinates: {r.latitude}, {r.longitude}")
        batch_requests.append(Request(
            custom_id=str(r.cp_id),
            params=MessageCreateParamsNonStreaming(
                model=AI_MODEL,
                max_tokens=1500,
                thinking={"type": "adaptive"},
                system=SYSTEM,
                tools=[{"type": "web_search_20260209", "name": "web_search"},
                       {"type": "web_fetch_20260209", "name": "web_fetch"}],
                messages=[{"role": "user", "content": user}],
            ),
        ))

    batch = client.messages.batches.create(requests=batch_requests)
    logger.info("Submitted batch %s for %d findings", batch.id, len(batch_requests))

    waited = 0
    while client.messages.batches.retrieve(batch.id).processing_status != "ended":
        if waited >= BATCH_MAX_WAIT_SECONDS:
            logger.warning("Batch %s not finished after %ds — rerun to collect", batch.id, waited)
            return
        time.sleep(BATCH_POLL_SECONDS)
        waited += BATCH_POLL_SECONDS

    rows = []
    for result in client.messages.batches.results(batch.id):
        if result.result.type != "succeeded":
            logger.warning("cp_id %s: batch result %s", result.custom_id, result.result.type)
            continue
        data = _extract_json(result.result.message)
        if not data:
            logger.warning("cp_id %s: could not parse JSON from response", result.custom_id)
            continue
        d = details_by_cp.get(str(result.custom_id), {})
        d.update({
            "issue": data.get("issue"),
            "suggested_postcode": data.get("suggested_postcode"),
            "suggested_address": data.get("suggested_address"),
            "description": data.get("description"),
            "ai_model": AI_MODEL,
            "request_id": result.result.message.id,
        })
        rows.append({"check_name": CHECK_NAME, "entity_id": str(result.custom_id), "details": json.dumps(d)})

    if not rows:
        logger.info("No parseable suggestions produced")
        return

    spark.createDataFrame(rows, schema="check_name string, entity_id string, details string") \
         .createOrReplaceTempView("dq_suggestions")
    spark.sql(f"""
        MERGE INTO {DQ_FINDINGS_TABLE} t
        USING dq_suggestions s ON t.check_name = s.check_name AND t.entity_id = s.entity_id
        WHEN MATCHED THEN UPDATE SET t.details = s.details
    """)
    logger.info("Merged %d AI suggestions into dq_findings.details", len(rows))


def main():
    parser = argparse.ArgumentParser(description="Postcode data-quality flagging + AI suggestions")
    parser.add_argument("--skip-ai", action="store_true",
                        default=os.getenv("DQ_SKIP_AI", "").lower() == "true",
                        help="Run deterministic flagging only; skip the Sonnet suggestion phase.")
    args = parser.parse_args()

    flag_anomalies()
    if args.skip_ai:
        logger.info("Skipping AI suggestion phase (--skip-ai)")
    else:
        suggest_fixes()


if __name__ == "__main__":
    main()
