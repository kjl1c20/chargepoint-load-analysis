# Model Decision Log

This document records the reasoning behind key decisions in the machine learning
pipeline. It complements the ETL decision log. Each decision is recorded as
a numbered **iteration** so the evolution of the approach is traceable.

---

## Preface — Why the SENSE-based v1 was retired (as of June 2026)

The original version of this project used session data from the Smart Energy
Data Service (SDR-SENSE) platform. That version has been fully retired for two
reasons:

**1. The SENSE dataset was incomplete.**
At the time of analysis, SENSE only exposed two months of CPS data — September
2024 and October 2025 — with a large gap in between. Two non-consecutive months
is far too little for any time-series or behavioural study: there is no stable
baseline, no seasonal context, and no way to distinguish a signal from noise.
Machine learning on this data would be fitting to artefacts of the sample, not
real patterns in the network.

**2. The original ML question turned out to be unnecessary.**
The v1 classifier was designed to predict whether an area "needs more chargers"
as a binary label. During review, it became clear that the label itself was
derived from the same utilisation signal used as a feature (target leakage),
and that once leakage was removed, a transparent percentile-ranked index
performed as well as the model — with no black-box complexity. The ML layer
added no information beyond what the ranking already communicated directly.

**What replaced it.**
The project pivoted to the full ChargePlace Scotland public session archive
(28 months, Jan 2024 – Apr 2026, ~3.16 million sessions). The primary
deliverable is now the **Demand-Pressure Index** (a transparent ranking) and
**usage-profile clustering** (an ML method that is genuinely robust to the
dataset's network-fragmentation problem and answers a question a simple ranking
cannot: *what kind* of charging does each area do?).

---

## Decision 1 — How `need_probability` is generated

**Affects:** the headline deliverable. `need_probability` is the per-district
score (0–1) that the output is ranked by — i.e. the prioritised list of
districts that "need more chargers". It is the model's estimated probability
that a district falls into the top quartile of **future** demand pressure.

### Context

After the credibility redesign, the model forecasts future demand pressure from
past behaviour (see the temporal split in `train_model.py`). The final step
attaches a probability to every district and sorts descending to produce the
ranked priority list. *How* that probability is produced materially affects how
trustworthy the ranking is.

---

### Iteration 1 — in-sample prediction

**What it does**

The model is trained on an 80% train split, then `predict_proba` is called on
the **full** dataset (including the rows it trained on):

```python
model.fit(X_train, y_train)
...
features["need_probability"] = model.predict_proba(X)[:, 1]
```

**Problem**

Because ~80% of the districts being scored were also used to train the model,
their probabilities reflect **memorisation, not generalisation**. XGBoost (200
trees, depth 4) effectively memorises a few hundred training rows and pushes them
to extreme, overconfident values.

**Evidence (latest run, 304 districts, 76 positive):**

| `need_probability` range | Districts |
|---|---|
| 0.0 – 0.1 | 208 (68%) |
| 0.1 – 0.9 | 49 |
| 0.9 – 1.0 | 47 (16%) |

- Median = 0.019; the distribution is **bimodal** (clustered near 0 and near 1).
- The extremes are partly genuine (past utilisation is a strong, separable
  predictor — baseline ROC-AUC ≈ 0.93) but partly **inflated by in-sample
  overconfidence**.

**Why this is a problem for the deliverable:** the ranking we publish is biased
by which districts happened to be in the training split, and the confidence
values are not honest probabilities. A district could rank high partly because
the model memorised it, not because it is genuinely high-pressure.

---

### Iteration 2 — out-of-fold predictions

**Status:** adopted — implemented in `train_model.py`.

**What it changes**

Generate `need_probability` with **out-of-fold** predictions so that every
district is scored by a model that never saw it during training:

```python
from sklearn.model_selection import cross_val_predict

features["need_probability"] = cross_val_predict(
    model, X, y, cv=cv, method="predict_proba"
)[:, 1]
```

Each district's score comes from the CV fold in which it was held out. The final
model is still fit on the full data for any future/new districts, but the
**published ranking uses the out-of-fold scores**.

**Why it is better**

- **No memorisation:** every score is a genuine out-of-sample estimate, so the
  ranking reflects generalisation rather than which districts were in the train
  split.
- **Better calibrated:** probabilities spread more honestly instead of collapsing
  to 0/1, so thresholds (e.g. the dashboard's `min_probability` filter) mean
  something.
- **Consistent with how we already report performance** — the headline metric is
  already cross-validated ROC-AUC (0.915 ± 0.042), so the published scores should
  come from the same out-of-fold regime.

**Trade-offs**

- Slightly more computation (fits the model once per fold).
- Out-of-fold scores depend on the fold split; fixing `random_state` keeps it
  reproducible.

**Decision:** adopt out-of-fold predictions for the published `need_probability`
ranking in iteration 2.

**Observed effect (same 304 districts):**

| `need_probability` range | Iteration 1 (in-sample) | Iteration 2 (out-of-fold) |
|---|---|---|
| 0.0 – 0.1 | 208 (68%) | 190 (62%) |
| 0.1 – 0.9 | 49 | 79 |
| 0.9 – 1.0 | 47 (16%) | 35 (12%) |

The mid-range roughly doubled and the >0.9 cluster shrank — the scores spread
more honestly instead of collapsing to the extremes, while the top of the
ranking (e.g. EH10 / City of Edinburgh) is unchanged.

---

## Decision 2 — The Demand-Pressure Index (primary deliverable)

**Affects:** `src/pressure_index.py` (built on `src/features.py`).

### Context

The temporally-validated model (Decision 1 work) showed that a transparent
ranking is as good as the ML for this problem. So the **primary deliverable is a
composite index**, not the model. It answers "where is charging infrastructure
under pressure right now" and is fully explainable.

### What goes into it

Pressure is built from the engine's measured signals:

- **Saturation rate** — share of available charge-point-time when *every*
  connector at a charge point is busy at once. The most direct evidence of
  *unmet* demand (drivers queue or leave).
- **Utilisation** — share of available connector-time that is occupied. Overall
  busy-ness.

Both come from session timestamps + real connector counts, using per-unit
availability windows (engine v2).

### Design choices

1. **Normalisation — percentile rank.** Each component is converted to a 0–1
   percentile rank before weighting. The raw rates are heavily skewed (most
   districts near 0, a few high); percentile rank is robust to that and keeps the
   score interpretable as "how this district compares to the rest".
2. **Weights — saturation-led (0.6 / 0.4).** Queuing is stronger evidence of a
   genuine shortfall than general busy-ness. Weights are explicit and tunable in
   `PRESSURE_WEIGHTS`; they need not sum to 1 (the score is normalised by their
   total).
3. **Revenue kept separate.** `total_revenue` / `revenue_per_connector`
   (`consumption_kwh × PricePerKWh`) are reported *alongside* the score as a
   commercial lens, never folded into "pressure". A district can be high-pressure
   and low-revenue (e.g. free chargers) — conflating them would hide that.

### Output

`{snapshot}_index.parquet`: per district `pressure_score` (0–1), `pressure_rank`,
the underlying metrics, and the revenue lens — sorted by pressure.

### Known limitation

Single-connector districts can rank highly because, with one connector,
saturation equals utilisation (one busy connector *is* "full"). This is
defensible (no redundancy = real pressure) but means the very top of the ranking
mixes small single-connector sites with genuine multi-connector hubs; read it
with `observed_connectors` in view.

> **Update (CPS pivot):** the index now operates at **local-authority** level on
> ChargePlace Scotland data (not postcode districts), location comes from the
> charge point table (`cp_id → local_authority` via geocoded `site_name`), and
> revenue is the CPS `amount` (£ paid) column directly. Output is
> `data/processed/pressure_index.parquet`.

---

## Decision 3 — Why demand *forecasting* was dropped (and what ML to do instead)

**Affects:** `src/forecast.py`. **Status:** the volume forecast is **not a credible
deliverable on this data**; the pressure index (Decision 2) remains the primary output.

### Context

For a planning audience, forecasting future demand ("where will we need more
chargers?", objective 2) was a goal. We built an LA-level monthly demand forecast
(Holt-Winters trend + 12-month seasonality, validated against a seasonal-naive
baseline) on the 28-month CPS series.

### Finding — the data cannot support a demand-volume forecast

The CPS network is **fragmenting**: under the 2025–26 transition, chargers are
migrating to other operators and so **leaving the CPS dataset**. The evidence:

| | active chargers | sessions / charger |
|---|---|---|
| 2024-07 → 2025-10 | ~3,600 | ~35 (flat) |
| 2026-04 | **1,659** | 38 |

- Total sessions fall sharply from Nov 2025, but **only because chargers leave the
  data** — demand *per charger* is flat (~36/month) throughout.
- A raw-count forecast therefore extrapolates a fake decline and would wrongly
  conclude *"Scotland needs fewer chargers"* — an artefact of network churn, not
  real demand.
- One further real break: a **coverage jump in Jul 2024** (2,426 → 3,511 active
  chargers — more chargers begin reporting).

> **Correction (2026-06-04):** an earlier version of this analysis reported Sep
> 2024 as a near-empty "bad file". That was wrong — it was a **cleaning bug**: the
> Sept file stores `duration` as a number of *seconds* while every other file uses
> `HH:MM:SS`, so the parser produced `NaT` and the invalid-session filter dropped
> all ~103k September rows. Fixed in `_normalise` (numeric duration → seconds);
> September is intact (102,776 sessions). The fragmentation conclusion is
> unaffected — it never depended on September.

Analogy: counting customers across a shop chain that is selling off half its
shops — the total drops, but people did not stop shopping.

### Why finer granularity does not help

Per-charge-point forecasting is **worse**: individual series are short and noisy,
and a charger that migrates off CPS simply goes to zero — you cannot forecast a
charger that has left the dataset. Granularity is not the problem; the churning,
trend-less dataset is. A genuine Scotland-wide *demand-growth* forecast would need
**all-operator** data, which CPS alone no longer represents.

### Decision

Drop the demand-volume forecast. Use ML for questions that are **robust to network
churn** (they study charging *behaviour* / *shape*, not extrapolated volumes):

1. **Usage-profile clustering** — group charge points into demand archetypes
   (commuter / destination / overnight) from their normalised temporal patterns.
   Churn-proof (uses profile shape, not totals); tells planners *what kind* of
   infrastructure each area needs.
2. **Session-level load prediction** — predict a session's energy/duration from
   time, connector type and location (supervised, ~3M independent rows). Supports
   grid/capacity planning; unaffected by network size.

---

## Decision 4 — Usage-profile clustering (the ML deliverable)

**Affects:** `src/cluster_profiles.py`. **Status:** implemented — chosen over the
forecast as the robust, planning-relevant ML.

### Method

Each charge point (with ≥ 30 sessions, 4,408 of 5,306) gets a behavioural
fingerprint — all *shape* features, so churn-proof:
- **When:** share of sessions in morning / daytime / evening / overnight
- **Pattern:** weekend ratio, rapid-connector share
- **Per session:** median duration, median energy

Features are standardised; **k-means** groups them, with **k chosen by silhouette**
(k = 6, score 0.32).

### Result — 6 archetypes

| Archetype | Chargers | Signature |
|---|---|---|
| Rapid top-up (daytime) | 862 | 43 min, 96% rapid — en-route / quick top-up |
| AC medium-stay (daytime) | 1,347 | ~3 h — shopping / destination |
| AC long-stay (morning) | 672 | ~4.5 h, morning peak — workplace |
| AC long-stay (evening) | 803 | ~7 h — evening / residential |
| AC all-day (daytime) | 593 | ~15 h — park-and-ride |
| AC long-stay (overnight) | 131 | overnight-heavy — residential |

### Planning value

Joining archetype to local authority shows **what kind** of charging each area
does — e.g. **Highland is rapid-top-up dominated** (en-route touring), while
**Glasgow/Edinburgh** are destination/workplace AC. This guides *what type* of
infrastructure to add where, which the demand-pressure index alone can't say.

### Caveats

- Silhouette 0.32 → boundaries are soft; archetypes are *tendencies*, not hard
  types. Fine for planning segmentation, don't over-read individual assignments.
- Labels are heuristic (editable in `label_cluster`).
- LA mix uses only geocoded charge points (~73%).

### Output

`data/processed/cp_clusters.parquet` — per charge point: profile features,
`cluster`, `archetype`, `local_authority`.

---
