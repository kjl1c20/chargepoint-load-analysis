# ⚡ EV Charging Infrastructure Needs Analysis (Scotland)

## Overview

This project uses real-world EV charging session data from Scotland’s Chargepoint network to identify **where additional EV charging infrastructure is needed**.

The goal is to build a machine learning model that classifies regions as:

> **Needs more chargers (1) vs Sufficient infrastructure (0)**

This helps support data-driven decisions for EV infrastructure planning.

---

## Objective

- Identify regions with high EV charging demand pressure
- Predict areas that likely require additional charging infrastructure
- Analyse spatial and temporal charging patterns across Scotland
- Produce actionable insights for infrastructure planning

---

## Problem Definition

Each region (or spatial cluster) is analysed over time to determine infrastructure strain.

### Target Variable
- **1 → Needs more chargers**
- **0 → Sufficient capacity**

### Key Metric

Utilisation rate is used as the main indicator of demand pressure:

:contentReference[oaicite:0]{index=0}

High utilisation indicates charger congestion and potential infrastructure shortage.

---

## Dataset

**Source:** Scotland Chargepoint Sessions Dataset

### Key Features
- Session start and end time
- Energy consumed (kWh)
- Charging duration
- Chargepoint ID
- Location / region (mapped or derived)
- Charger type (AC / DC where available)

### Optional External Data
- Population density
- EV ownership statistics
- Charger inventory per region

---

## Methodology

### 1. Data Processing
- Clean raw session data
- Handle missing/invalid records
- Extract time features (hour, day, month)
- Map chargepoints to regions

### 2. Feature Engineering
- Sessions per region
- Sessions per charger
- Peak hourly demand
- Utilisation rate
- Temporal patterns (weekday/weekend, peak hours)

### 3. Label Creation
Regions are labelled as “needs chargers” based on:
- High utilisation
- Peak demand exceeding capacity thresholds

---

### Machine Learning Models
- Logistic Regression (baseline)
- Random Forest
- XGBoost (primary model)

---

### Evaluation Metrics
- Accuracy
- Precision / Recall
- F1-score
- ROC-AUC

(Recall is especially important for detecting overloaded regions.)

---

## Outputs

- Ranked list of high-priority regions for new chargers
- Heatmap of infrastructure pressure across Scotland
- Temporal demand patterns (peak hours, weekday vs weekend usage)

---

## Tech Stack

- Python
- pandas / NumPy
- scikit-learn
- XGBoost / LightGBM
- matplotlib / seaborn
- GeoPandas / Folium (mapping)
- Streamlit (optional dashboard)

---

## How to Run
clone this repo

pip install -r requirements.txt

python src/models/train.py