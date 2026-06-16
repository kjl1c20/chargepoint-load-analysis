# Postcode Data-Quality Runbook

How we detect and correct wrong postcodes in the CPS locations feed (e.g. The State
Hospital, Carstairs arriving as `M11 8RP` instead of `ML11 8RP`). Bronze is never edited —
corrections are applied **fix-on-read** when building Silver.

## How it works

Each charge point has three location signals; the odd one out is the error:

- **A1** — feed postcode area (Silver `postcode`)
- **A2** — postcode area reverse-geocoded from the coordinates (postcodes.io)
- **A3** — what `site_name` actually is (resolved by AI only when needed)

`dq_postcodes.py` flags anomalies deterministically (A1 vs A2 vs the 16-area Scottish
allowlist) into the generic register `reference.dq_findings`, then a Claude Sonnet batch
merges a suggested fix into each finding's `details` JSON. A human approves the fix into the
`POSTCODE_OVERRIDES` mapping in `build_charge_points.py`, applied on the next rebuild.

`dq_findings` is **check-agnostic**: fixed columns (`check_name`, `entity_id`, `message`,
`status`, timestamps) plus a `details` JSON for check-specific fields. Postcode findings use
`check_name = 'charge_points.postcode_triangulation'`; future checks reuse the table.

## Pipeline order

```
setup.sql                         # once — creates reference schema + tables (idempotent)  [Databricks]
build_charge_points.py            # Silver charge_points (applies any approved overrides)   [Databricks]
dq_postcodes.py [--skip-ai]       # flag anomalies → dq_findings; --skip-ai skips the AI     [LOCAL]
   → human review (below)
build_charge_points.py            # re-run: approved overrides now flow into Silver          [Databricks]
site_pressure.py                  # Gold reflects corrected postcode_area                    [Databricks]
```

> **`dq_postcodes.py` runs locally, not on Databricks.** Serverless compute blocks outbound
> calls to postcodes.io and api.anthropic.com, so this job runs on your machine and talks to
> Databricks over `databricks-sql-connector` (same as the dashboard). It needs in `.env`:
> `DATABRICKS_SERVER_HOSTNAME` (or `DATABRICKS_HOST`), `DATABRICKS_HTTP_PATH`, `DATABRICKS_TOKEN`,
> and — for the AI phase — `ANTHROPIC_API_KEY`. Run: `poetry run python src/dq_postcodes.py [--skip-ai]`.

## Reviewing and approving (the human step)

1. **Look at what was flagged + suggested** (postcode fields are inside `details`):
   ```sql
   SELECT entity_id AS cp_id,
          message,
          get_json_object(details, '$.verdict')           AS verdict,
          get_json_object(details, '$.coord_postcode')    AS coord_postcode,
          get_json_object(details, '$.suggested_postcode') AS suggested_postcode,
          get_json_object(details, '$.suggested_address')  AS suggested_address,
          get_json_object(details, '$.description')        AS description
   FROM chargepoint_analysis.reference.dq_findings
   WHERE check_name = 'charge_points.postcode_triangulation' AND status = 'open'
   ORDER BY verdict;
   ```
   `verdict` is one of: `postcode_not_scottish`, `area_mismatch`, `postcode_missing`.
   The AI suggestion is a *proposal* — sanity-check it before approving.

2. **Approve** — add the correction to the curated mapping `POSTCODE_OVERRIDES` in
   `src/build_charge_points.py` and commit (the git commit is the audit trail — who/when/why):
   ```python
   POSTCODE_OVERRIDES = {
       "<cp_id>": "<correct_postcode>",  # <reason>
   }
   ```
   The finding auto-resolves on the next `dq_postcodes.py` run once the corrected postcode
   flows into Silver.

3. **Dismiss** a false positive (feed was actually fine — keeps it from re-opening):
   ```sql
   UPDATE chargepoint_analysis.reference.dq_findings
   SET status = 'dismissed', resolved_at = current_timestamp()
   WHERE check_name = 'charge_points.postcode_triangulation' AND entity_id = '<cp_id>';
   ```

4. **Apply** — re-run `build_charge_points.py`. Silver `postcode` now shows the corrected
   value with `postcode_source = 'override'`; re-run `site_pressure.py` so Gold and the
   dashboard pick up the corrected `postcode_area`. The next `dq_postcodes.py` run sees the
   site is no longer anomalous and sets its finding to `resolved` automatically.

## Notes

- **Re-running `dq_postcodes.py` is safe.** New anomalies are inserted; still-open ones are
  left untouched (preserving any AI suggestion in `details`); fixed ones are auto-`resolved`;
  `dismissed` ones never re-open. Once an override is applied, the charge point stops being
  flagged (A1 now agrees with A2).
- **`--skip-ai`** runs the deterministic flagging only — no `ANTHROPIC_API_KEY`, no spend.
  Use it to populate the queue, then run the full job (or just the AI phase later) to add
  suggestions.
- **Border false-positives:** comparison is at *area* level, so a coordinate that resolves to
  a neighbouring area can raise `area_mismatch`. Treat the verdict as advisory and confirm
  before approving.
- **Cost:** the AI phase uses the Message Batches API (50% off) and runs only on flagged
  rows, so it is a handful of `claude-sonnet-4-6` calls per sweep.
