# ETL Decision Log

This document records the reasoning behind key decisions made in the ETL pipeline. It is intended to provide traceability for design choices that are not obvious from the code alone.

---

## Decision 1 — Use `Postcode` instead of `City` as the geographic grouping field

**Status:** Adopted  
**Affects:** `cleaner.py` — missing-value filter step

### Context

The raw dataset contains two location fields that could serve as the primary geographic grouping unit: `City` and `Postcode`. The original pipeline dropped rows where `City` was null, which was used as the geographic identifier downstream.

### Problem with `City`

**1. Data inaccuracy**

The `City` field contains inconsistent and unreliable values (as shown in the EDA process). During string normalisation alone, **31 distinct spelling variants** of city names were collapsed into canonical forms (e.g. mixed casing, trailing whitespace, abbreviations). Beyond normalisation, the field itself is often incorrect — chargepoints are frequently assigned to a city that does not accurately reflect the actual city. The column include entries like 'Aberdeenshire' and 'Scotland'. This makes `City` unsuitable as a reliable grouping key for spatial analysis.

### Why `Postcode`

- **Higher accuracy:** Postcodes are assigned at the chargepoint registration level and are a more reliable indicator of physical location than the freeform `City` field.
- **Lower missingness:** The `Postcode` column has fewer null values, preserving more usable records through the cleaning pipeline.
- **Finer granularity:** Postcodes provide a more precise spatial unit, which is better suited to infrastructure gap analysis across Scotland.

### Decision

Drop `City` as a required field. Get city name through postcode.
