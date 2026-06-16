"""Postcode data-quality: flag anomalies by triangulation, then suggest fixes with Sonnet.

Runs **locally** (not on Databricks Serverless, whose network egress blocks postcodes.io and
api.anthropic.com). Talks to Databricks over databricks-sql-connector — same pattern as
harvest_locations.py and dashboard.py — and makes the external calls from your machine.

Writes to the generic register `reference.dq_findings`: thin fixed columns plus a `details`
JSON for check-specific fields, so new checks reuse the table by writing a different
`check_name` + `details`.

  flag_anomalies()  — deterministic. Reverse-geocode each charge point's coordinates
                      (postcodes.io), compare feed area (A1) vs coord area (A2) vs the
                      Scottish allowlist, and upsert findings (status='open'). Findings no
                      longer flagged are auto-resolved; dismissed ones never re-open.

  suggest_fixes()   — AI (skippable via --skip-ai / DQ_SKIP_AI). For each open finding with
                      no suggestion yet, a grounded Claude Sonnet call proposes a fix and
                      merges {issue, suggested_postcode, suggested_address, description} into
                      the finding's `details`.

A human applies an approved correction to the POSTCODE_OVERRIDES mapping in
build_charge_points.py; it picks it up on the next Silver rebuild. Bronze/Silver are never
touched here.

Run:  poetry run python src/dq_postcodes.py [--skip-ai]
Needs in .env:  DATABRICKS_SERVER_HOSTNAME (or DATABRICKS_HOST), DATABRICKS_HTTP_PATH,
                DATABRICKS_TOKEN, and (AI phase only) ANTHROPIC_API_KEY.
"""

import os
import re
import json
import logging
import argparse

import requests
from databricks import sql as dbsql
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

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
AI_MAX_TOKENS = int(os.getenv("DQ_AI_MAX_TOKENS", "4096"))


# ============================================================
# Databricks SQL connection (parameterized — no string interpolation of values)
# ============================================================

def _connect():
    host = (os.getenv("DATABRICKS_SERVER_HOSTNAME") or os.getenv("DATABRICKS_HOST") or "")
    host = host.replace("https://", "").replace("http://", "").rstrip("/")
    http_path, token = os.getenv("DATABRICKS_HTTP_PATH"), os.getenv("DATABRICKS_TOKEN")
    if not (host and http_path and token):
        raise RuntimeError(
            "Missing Databricks connection settings. Set DATABRICKS_SERVER_HOSTNAME "
            "(or DATABRICKS_HOST), DATABRICKS_HTTP_PATH and DATABRICKS_TOKEN in .env."
        )
    return dbsql.connect(server_hostname=host, http_path=http_path, access_token=token)


def _query(sql: str, params: dict | None = None):
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params) if params else cur.execute(sql)
        return cur.fetchall_arrow().to_pandas()


def _execute(operations: list[tuple[str, dict]]):
    """Run (sql, params) write statements in one session. Delta DML is atomic per statement."""
    if not operations:
        return
    with _connect() as conn, conn.cursor() as cur:
        for sql, params in operations:
            cur.execute(sql, params) if params else cur.execute(sql)


# ============================================================
# helpers (pure)
# ============================================================

def _area(postcode) -> str | None:
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
    if not feed_area:
        return "postcode_missing"
    if feed_area not in allowlist:
        return "postcode_not_scottish"
    if coord_area and coord_area in allowlist and coord_area != feed_area:
        return "area_mismatch"
    return None


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
    allowlist = set(_query(f"SELECT area_code FROM {AREAS_TABLE}")["area_code"])
    if not allowlist:
        raise RuntimeError(
            f"{AREAS_TABLE} is empty — run setup.sql to seed the Scottish postcode areas "
            "before validating (an empty allowlist would flag every site)."
        )

    cps = _query(f"""
        SELECT cp_id,
               first(site_name, true) AS site_name,
               first(postcode, true)  AS postcode,
               first(latitude, true)  AS latitude,
               first(longitude, true) AS longitude
        FROM {SILVER_TABLE} GROUP BY cp_id
    """)
    if cps.empty:
        logger.warning("%s has no charge points — nothing to validate", SILVER_TABLE)
        return
    logger.info("Checking %d charge points", len(cps))

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

    existing = _query(
        f"SELECT entity_id, status FROM {DQ_FINDINGS_TABLE} WHERE check_name = :cn",
        {"cn": CHECK_NAME},
    )
    status_by_id = dict(zip(existing["entity_id"], existing["status"])) if not existing.empty else {}
    flagged_ids = set(flagged["cp_id"])

    ops: list[tuple[str, dict]] = []
    for r in flagged.to_dict("records"):
        eid = r["cp_id"]
        message = (f"Feed postcode {r['postcode']!r} (area {r['feed_area']!r}) — "
                   f"{VERDICT_MSG.get(r['verdict'], r['verdict'])}.")
        details = json.dumps({
            "verdict": r["verdict"], "site_name": r["site_name"],
            "feed_postcode": r["postcode"], "feed_area": r["feed_area"],
            "coord_postcode": r["coord_postcode"], "coord_area": r["coord_area"],
        })
        prior = status_by_id.get(eid)
        if prior is None:  # new anomaly
            ops.append((
                f"INSERT INTO {DQ_FINDINGS_TABLE} "
                "(check_name, entity_id, message, details, status, detected_at) "
                "VALUES (:cn, :eid, :msg, :details, 'open', current_timestamp())",
                {"cn": CHECK_NAME, "eid": eid, "msg": message, "details": details},
            ))
        elif prior == "resolved":  # regression — reopen, refresh
            ops.append((
                f"UPDATE {DQ_FINDINGS_TABLE} SET status='open', resolved_at=NULL, "
                "message=:msg, details=:details WHERE check_name=:cn AND entity_id=:eid",
                {"cn": CHECK_NAME, "eid": eid, "msg": message, "details": details},
            ))
        # prior in ('open','dismissed') → leave untouched (preserves AI suggestion / dismissal)

    for eid, st in status_by_id.items():  # auto-resolve no-longer-flagged
        if st == "open" and eid not in flagged_ids:
            ops.append((
                f"UPDATE {DQ_FINDINGS_TABLE} SET status='resolved', resolved_at=current_timestamp() "
                "WHERE check_name=:cn AND entity_id=:eid",
                {"cn": CHECK_NAME, "eid": eid},
            ))

    _execute(ops)
    logger.info("dq_findings updated: %d statement(s) applied", len(ops))


# ============================================================
# phase 2 — AI fix suggestions (Sonnet, grounded) → details
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
TOOLS = [{"type": "web_search_20260209", "name": "web_search"},
         {"type": "web_fetch_20260209", "name": "web_fetch"}]


def _ask(client, user: str):
    """One grounded Sonnet call; resume through server-tool pauses."""
    messages = [{"role": "user", "content": user}]
    for _ in range(5):
        resp = client.messages.create(
            model=AI_MODEL, max_tokens=AI_MAX_TOKENS, thinking={"type": "adaptive"},
            system=SYSTEM, tools=TOOLS, messages=messages,
        )
        if resp.stop_reason != "pause_turn":
            return resp
        messages = messages + [{"role": "assistant", "content": resp.content}]
    return resp


def suggest_fixes():
    import anthropic

    if not os.getenv("ANTHROPIC_API_KEY"):
        logger.warning("ANTHROPIC_API_KEY not set — skipping AI suggestions "
                       "(deterministic findings already written). Set it in .env to enable, "
                       "or pass --skip-ai to silence this.")
        return

    pending = _query(f"""
        SELECT f.entity_id AS cp_id, f.details AS details, cp.latitude, cp.longitude
        FROM {DQ_FINDINGS_TABLE} f
        LEFT JOIN (SELECT cp_id, first(latitude, true) AS latitude, first(longitude, true) AS longitude
                   FROM {SILVER_TABLE} GROUP BY cp_id) cp ON f.entity_id = cp.cp_id
        WHERE f.check_name = :cn AND f.status = 'open'
          AND get_json_object(f.details, '$.suggested_postcode') IS NULL
    """, {"cn": CHECK_NAME})

    if pending.empty:
        logger.info("No open findings need an AI suggestion")
        return

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    ops: list[tuple[str, dict]] = []
    for r in pending.itertuples():
        d = json.loads(r.details)
        user = (f"Site name: {d.get('site_name')}\nPostcode on record: {d.get('feed_postcode')}\n"
                f"Postcode from coordinates: {d.get('coord_postcode')}\n"
                f"Coordinates: {r.latitude}, {r.longitude}")
        try:
            resp = _ask(client, user)
        except anthropic.APIError as e:  # rate limit / network / server — skip this one
            logger.warning("cp_id %s: Anthropic call failed (%s) — skipping", r.cp_id, e)
            continue

        data = _extract_json(resp)
        if not data:
            logger.warning("cp_id %s: could not parse JSON (stop_reason=%s)", r.cp_id, resp.stop_reason)
            continue

        d.update({
            "issue": data.get("issue"),
            "suggested_postcode": data.get("suggested_postcode"),
            "suggested_address": data.get("suggested_address"),
            "description": data.get("description"),
            "ai_model": AI_MODEL,
            "request_id": resp.id,
        })
        ops.append((
            f"UPDATE {DQ_FINDINGS_TABLE} SET details=:details WHERE check_name=:cn AND entity_id=:eid",
            {"details": json.dumps(d), "cn": CHECK_NAME, "eid": str(r.cp_id)},
        ))

    _execute(ops)
    logger.info("Merged %d/%d AI suggestion(s) into dq_findings.details", len(ops), len(pending))


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
