# Model Decision Log

Notes on key choices in the pipeline — what we tried, what broke, and why we landed where we did.

---

## Why the original v1 (SENSE-based) approach was retired

SENSE only exposed two non-consecutive months of CPS data (Sep 2024, Oct 2025) — too little for a reliable study. The ML classifier also had target leakage: once fixed, a transparent percentile ranking matched its performance. The project moved to the full CPS public archive; the classifier was replaced by the Demand-Pressure Index.

---

## Decision 1 — The Demand-Pressure Index

After the temporal validation showed the simple ranking matched the model in ROC-AUC, we made the index the primary deliverable rather than the classifier.

> **Update (2026-06-13):** the index now runs at **charge-point (site) grain**, not local authority — see Decision 2. The weighted-percentile definition below is unchanged; only the unit being ranked changed (sites instead of LAs).

Two signals per charge point:
- **Saturation rate** (weight 0.6) — share of charge-point time when every connector is simultaneously busy. The most direct evidence of unmet demand.
- **Utilisation** (weight 0.4) — share of available connector-time that's occupied.

Both are percentile-ranked before weighting, because the raw rates are heavily skewed.

Known limit: single-connector charge points can rank high because saturation equals utilisation when there's only one connector. Not wrong (no redundancy is real pressure), but read the top of the ranking with connector counts in view.

---

## Decision 2 — Site grain over local-authority grain (2026-06-13)

The headline question — *where can we build more chargers?* — is a **siting** decision,
and a local authority is the wrong unit for it. We moved the pressure index from LA grain
to **charge-point (`cp_id`) grain**. Two reasons:

1. **LA aggregation dilutes a concentrated signal.** Pressure is concentrated in a handful
   of saturated sites surrounded by idle ones (median utilisation <1%). Averaging into an
   LA mean drowns the saturated sites exactly where the signal is strongest.
2. **It invites the ecological fallacy.** "West Dunbartonshire is rank #1" doesn't tell a
   planner *which* site to expand. The site is the unit at which the decision is actually
   made.

This was a *removal*, not new machinery: `build_cp_metrics` already computed saturation,
utilisation, and connector counts per `cp_id`; the LA roll-up (`aggregate_to_la`) was a
lossy layer on top. The ranking function (`build_pressure_index`) is grain-agnostic, so it
ranks sites unchanged. `aggregate_to_la`, `cluster_profiles.py`, and `pressure_index.py`
were removed; the new entry point is [site_pressure.py](../src/site_pressure.py).

**Decisions taken:**
- **Single-connector sites flagged, not dropped.** At k=1 saturation equals utilisation,
  so the weighted score double-counts one quantity. No redundancy is real pressure, so
  they stay — flagged via `single_connector` and read with `n_connectors` in view.
- **Sites absent from the locations feed are dropped**. `harvest_locations.py`
  fetches coordinates from the CPS feed; charge points that appear in sessions but have no
  matching entry in that feed have no coordinates and are excluded from the ranking.

**Scope boundary:** this ranks where to **expand existing strained sites**. It cannot see
net-new demand in places with no chargers (no sessions = invisible) — that would need
demand denominators (EV registrations / population), which CPS session data alone lacks.
