# Model Decision Log

Notes on key choices in the pipeline — what we tried, what broke, and why we landed where we did.

---

## Why the original v1 (SENSE-based) approach was retired

The v1 used data from the Smart Energy Data Service (SDR-SENSE). Two things killed it.

SENSE only exposed two months of CPS data — September 2024 and October 2025 — with a year-long gap in between. That's not enough to study behaviour or build anything reliable.

The ML question was also circular. The v1 classifier predicted whether an area "needs more chargers" as a binary label, but that label was derived from the same utilisation signal used as a feature. Once the leakage was removed, a transparent percentile ranking performed just as well. There was no reason to have a model.

The project moved to the full CPS public archive (28 months, ~3.16M sessions). Primary deliverables are now the Demand-Pressure Index and usage-profile clustering.

---

## Decision 1 — How `need_probability` is generated *(retired — kept as history)*

> This decision applied to the v1 temporal classifier (`train_model.py`), which has since been removed. The current pipeline has no predictive model — the primary deliverable is the Demand-Pressure Index (Decision 2) and clustering (Decision 4). This section is kept as a record of what was tried and why it was dropped.

The headline output was a ranked list of districts by estimated future demand pressure.

**First attempt (in-sample):** `model.fit(X_train, y_train)` then `predict_proba` on the full dataset including training rows. XGBoost memorised ~80% of districts and pushed their probabilities to extreme values — the distribution was bimodal (68% near 0, 16% near 1) with almost nothing in the middle. Not honest.

**Second attempt:** out-of-fold predictions via `cross_val_predict`. Every district's score came from a fold it wasn't trained on. The mid-range roughly doubled and the >0.9 cluster shrank from 16% to 12%.

**Why it was retired:** even with the leakage fixed, a transparent percentile ranking matched the model's ROC-AUC. There was no justification for the added complexity. The classifier and `train_model.py` were removed; the index became the primary deliverable.

---

## Decision 2 — The Demand-Pressure Index

After the temporal validation showed the simple ranking matched the model in ROC-AUC, we made the index the primary deliverable rather than the classifier.

> **Update (2026-06-13):** the index now runs at **charge-point (site) grain**, not local authority — see Decision 5. The weighted-percentile definition below is unchanged; only the unit being ranked changed (sites instead of LAs).

Two signals per local authority:
- **Saturation rate** (weight 0.6) — share of charge-point time when every connector is simultaneously busy. The most direct evidence of unmet demand.
- **Utilisation** (weight 0.4) — share of available connector-time that's occupied.

Both are percentile-ranked before weighting, because the raw rates are heavily skewed. Revenue is reported alongside but never folded in — a district can be high-pressure and low-revenue (e.g. free chargers) and conflating them would hide that.

Known limit: single-connector charge points can rank high because saturation equals utilisation when there's only one connector. Not wrong (no redundancy is real pressure), but read the top of the ranking with connector counts in view.

---

## Decision 3 — Why demand forecasting was dropped

The CPS network is fragmenting. Chargers have been migrating to other operators since late 2025 and leaving the dataset. Active chargers roughly halved from Nov 2025 to Apr 2026 (3,584 → 1,659), while demand per charger stayed flat (~36 sessions/month). A raw-count forecast would extrapolate a decline that isn't real and conclude Scotland needs fewer chargers — which is backwards.

Going finer-grained to per-charger forecasting makes it worse: short noisy series, and a charger that migrates just goes to zero.

Think of it like counting footfall across a shop chain that's selling off half its shops. The total drops, but people didn't stop shopping.

The volume forecast is not a credible deliverable on this data. A real Scotland-wide demand forecast would need all-operator data, which CPS alone no longer represents.

> **Correction (2026-06-04):** an earlier version of this analysis reported Sep 2024 as a near-empty bad file. That was a cleaning bug — the Sept file stores `duration` as a number of seconds while every other file uses `HH:MM:SS`, so the parser silently dropped all ~103k September rows. Fixed in `_normalise`. The fragmentation conclusion is unaffected.

---

## Decision 4 — Usage-profile clustering / behaviour archetypes *(removed entirely 2026-06-13)*

> **Removed entirely.** The archetype concept is gone from the project — no clustering and
> no rule-based behaviour labels. This happened in two steps:
>
> **Step 1 — KMeans → rules.** KMeans was retired first because the cluster *naming* was
> done by four threshold rules on the centroids; if four rules can name every cluster, the
> rules carry the segmentation and the model adds nothing. Compounded by the fact that the
> only place clustering fed a decision — the LA planning table's *modal* archetype — was a
> uniform "AC public / retail" in every LA (catch-all rule + a mode over it). A column that
> never varies answers nothing.
>
> **Step 2 — rules removed too.** The rule-based per-site behaviour label
> (`label_behaviour`) and the feature builder behind it (`build_profiles`) were then
> dropped entirely. The behaviour/"what to build" lens did not earn its place against the
> project's actual question — *where to expand strained sites* — which the pressure
> ranking answers on its own. The dashboard is now pure pressure ranking + per-site
> performance; `site_pressure.parquet` no longer carries any behaviour columns.
>
> Net: the project carries **no archetype deliverable**. History of the clustering
> approach kept below for the record.

Chosen as the ML deliverable instead of the forecast because it studies the *shape* of demand, not volume — so network churn doesn't affect it.

Each charge point above a minimum session threshold gets a fingerprint: time-of-day shares (morning, midday, evening — overnight is dropped as it is linearly redundant with the other three), weekend ratio, rapid-connector share, median duration, median energy. Features are standardised. K-means with k chosen by silhouette score.

Current archetypes: Rapid top-up, AC commuter, AC public / retail, AC depot / long-stay.

Planning value: the pressure index tells you *where* to build; clustering tells you *what* to build. Silhouette scores in this range mean soft boundaries — these are tendencies, not hard types. Fine for planning segmentation, but don't over-read individual assignments.

---

## Decision 5 — Site grain over local-authority grain (2026-06-13)

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
- **Session floor raised to 100** (`MIN_SESSIONS_SITE`, was 50) — at site grain a noisy
  low-traffic charger can top the saturation ranking, so a stricter floor matters more.
- **Single-connector sites flagged, not dropped.** At k=1 saturation equals utilisation,
  so the weighted score double-counts one quantity. No redundancy is real pressure, so
  they stay — flagged via `single_connector` and read with `n_connectors` in view.
- **Ungeocoded sites dropped** (~27% geocoding miss). Geography is required to place a
  charger; ungeocoded sites are excluded from the ranking, same as the old LA pipeline.
  Coverage remains a known issue tracked in the README, not solved here.

**Scope boundary:** this ranks where to **expand existing strained sites**. It cannot see
net-new demand in places with no chargers (no sessions = invisible) — that would need
demand denominators (EV registrations / population), which CPS session data alone lacks.
