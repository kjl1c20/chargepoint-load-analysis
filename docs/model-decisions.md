# Model Decision Log

Notes on key choices in the pipeline — what we tried, what broke, and why we landed where we did.

---

## Why the original v1 (SENSE-based) approach was retired

The v1 used data from the Smart Energy Data Service (SDR-SENSE). Two things killed it.

SENSE only exposed two months of CPS data — September 2024 and October 2025 — with a year-long gap in between. That's not enough to study behaviour or build anything reliable.

The ML question was also circular. The v1 classifier predicted whether an area "needs more chargers" as a binary label, but that label was derived from the same utilisation signal used as a feature. Once the leakage was removed, a transparent percentile ranking performed just as well. There was no reason to have a model.

The project moved to the full CPS public archive (28 months, ~3.16M sessions). Primary deliverables are now the Demand-Pressure Index and usage-profile clustering.

---

## Decision 1 — How `need_probability` is generated

The headline output is a ranked list of districts by estimated future demand pressure. How the probability is produced matters.

**First attempt (in-sample):** `model.fit(X_train, y_train)` then `predict_proba` on the full dataset including training rows. XGBoost memorised ~80% of districts and pushed their probabilities to extreme values — the distribution was bimodal (68% near 0, 16% near 1) with almost nothing in the middle. Not honest.

**What we use now:** out-of-fold predictions via `cross_val_predict`. Every district's score comes from a fold it wasn't trained on. The mid-range roughly doubled and the >0.9 cluster shrank from 16% to 12%. Top of the ranking (e.g. EH10/Edinburgh) unchanged — the ranking was fine, the confidence values weren't.

---

## Decision 2 — The Demand-Pressure Index

After the temporal validation showed the simple ranking matched the model in ROC-AUC, we made the index the primary deliverable rather than the classifier.

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

## Decision 4 — Usage-profile clustering

Chosen as the ML deliverable instead of the forecast because it studies the *shape* of demand, not volume — so network churn doesn't affect it.

Each charge point (≥30 sessions) gets a fingerprint: time-of-day shares, weekend ratio, rapid-connector share, median duration, median energy. Features are standardised. K-means with k chosen by silhouette score (k=6, score 0.32).

Six archetypes:

| Archetype | Chargers | Signature |
|---|---|---|
| Rapid top-up (daytime) | 862 | 43 min, 96% rapid — en-route |
| AC medium-stay (daytime) | 1,347 | ~3 h — shopping/destination |
| AC long-stay (morning) | 672 | ~4.5 h, morning peak — workplace |
| AC long-stay (evening) | 803 | ~7 h — evening/residential |
| AC all-day (daytime) | 593 | ~15 h — park-and-ride |
| AC long-stay (overnight) | 131 | overnight-heavy — residential |

Planning value: Highland is rapid-top-up dominated (en-route touring), cities are destination/workplace AC. The pressure index tells you *where* to build; clustering tells you *what* to build.

Caveat: silhouette 0.32 means soft boundaries — these are tendencies, not hard types. Fine for planning segmentation, but don't over-read individual assignments.
