"""
Scotland EV Charging — Infrastructure Planning Dashboard.

For a planning audience. Five tabs:
  1. Overview      — network state + honest temporal context (fragmentation)
  2. Demand Pressure — where is the network strained now (pressure index)
  3. Demand Archetypes — what KIND of charging happens where (clustering)
  4. Planning Priorities — where + what to build (pressure x archetype)
  5. Data & Method  — coverage, caveats, methodology

Reads the processed outputs (pressure_index, cp_clusters) and aggregates the
clean sessions live with caching. (A dbt precompute layer is planned later.)

Run:  poetry run streamlit run src/dashboard.py
"""

from pathlib import Path

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import folium
from folium.plugins import HeatMap
from streamlit_folium import st_folium
import streamlit as st


st.set_page_config(page_title="Scotland EV Charging — Planning", layout="wide")

CLEAN_PATH = Path("./data/clean/cps_sessions_clean.parquet")
INDEX_PATH = Path("./data/processed/pressure_index.parquet")
CLUSTERS_PATH = Path("./data/processed/cp_clusters.parquet")

TIME_SHARES = ["pct_morning", "pct_midday", "pct_evening", "pct_overnight"]
DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

for p in (INDEX_PATH, CLUSTERS_PATH):
    if not p.exists():
        st.error(f"Missing {p}. Run pressure_index.py and cluster_profiles.py first.")
        st.stop()


# ============================================================
# cached loaders / aggregations
# ============================================================

@st.cache_data
def load_index():
    return pd.read_parquet(INDEX_PATH)


@st.cache_data
def load_clusters():
    return pd.read_parquet(CLUSTERS_PATH)


@st.cache_data
def load_monthly():
    df = pd.read_parquet(CLEAN_PATH, columns=["start_time", "cp_id", "consumption_kwh"])
    df["month"] = pd.to_datetime(df["start_time"]).dt.to_period("M").astype(str)
    g = df.groupby("month").agg(
        sessions=("cp_id", "size"),
        active_chargers=("cp_id", "nunique"),
        energy_mwh=("consumption_kwh", lambda x: x.sum() / 1000)
    ).reset_index()
    g["sessions_per_charger"] = g["sessions"] / g["active_chargers"]
    return g


@st.cache_data
def load_hour_dow():
    t = pd.to_datetime(pd.read_parquet(CLEAN_PATH, columns=["start_time"])["start_time"])
    h = pd.DataFrame({"hour": t.dt.hour, "dow": t.dt.dayofweek})
    return h.groupby(["dow", "hour"]).size().unstack(fill_value=0).reindex(range(7))


@st.cache_data
def load_totals():
    df = pd.read_parquet(CLEAN_PATH, columns=["cp_id", "consumption_kwh", "amount", "start_time"])
    return {
        "sessions": len(df),
        "chargers": df["cp_id"].nunique(),
        "energy_mwh": df["consumption_kwh"].sum() / 1000,
        "revenue": df["amount"].sum(),
        "date_min": pd.to_datetime(df["start_time"]).min().date(),
        "date_max": pd.to_datetime(df["start_time"]).max().date(),
    }


index = load_index()
clusters = load_clusters()

# dominant archetype per local authority (for map + priorities)
geo_clusters = clusters.dropna(subset=["local_authority"])
dominant = (
    geo_clusters.groupby("local_authority")["archetype"]
    .agg(lambda s: s.mode().iloc[0])
    .rename("dominant_archetype")
)

st.title("Scotland EV Charging Profile Analysis")
st.caption("ChargePlace Scotland public network · demand pressure & charging archetypes aggregated by local authority")

tab1, tab2, tab3, tab4, tab5 = st.tabs(
    ["Overview", "Demand Pressure", "Demand Archetypes", "Planning Priorities", "Data & Method"]
)

# ============================================================
# Tab 1 — Overview
# ============================================================

with tab1:
    t = load_totals()
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Charge points", f"{t['chargers']:,}")
    c2.metric("Sessions", f"{t['sessions']:,}")
    c3.metric("Energy", f"{t['energy_mwh']:,.0f} MWh")
    c4.metric("Avg utilisation", f"{index['utilisation'].mean():.1%}")
    c5.metric("Revenue (context)", f"£{t['revenue']:,.0f}")

    st.subheader("Demand over time")
    st.warning(
        "**Read with care:** the recent drop in total sessions is the CPS network "
        "*shrinking* (chargers migrating to other operators), **not** demand falling — "
        "demand *per charger* stays flat. Plotted together below."
    )
    m = load_monthly()
    fig = go.Figure()
    fig.add_bar(x=m["month"], y=m["sessions"], name="Total sessions", marker_color="#9ecae1")
    fig.add_scatter(x=m["month"], y=m["active_chargers"], name="Active chargers",
                    yaxis="y2", line=dict(color="crimson"))
    fig.update_layout(
        yaxis=dict(title="Sessions"),
        yaxis2=dict(title="Active chargers", overlaying="y", side="right", showgrid=False),
        legend=dict(orientation="h"), height=380, margin=dict(t=10)
    )
    st.plotly_chart(fig, use_container_width=True)

    fig_pc = px.line(m, x="month", y="sessions_per_charger",
                     title="Sessions per charger (real per-charger demand — flat)")
    fig_pc.update_layout(height=300, margin=dict(t=40))
    st.plotly_chart(fig_pc, use_container_width=True)

    st.subheader("When is the network busiest?")
    hd = load_hour_dow()
    fig_h = px.imshow(hd.values, x=list(range(24)), y=DOW, aspect="auto",
                      color_continuous_scale="OrRd", labels=dict(x="Hour", y="", color="Sessions"))
    fig_h.update_layout(height=320, margin=dict(t=10))
    st.plotly_chart(fig_h, use_container_width=True)

# ============================================================
# Tab 2 — Demand Pressure
# ============================================================

with tab2:
    st.subheader("Demand-pressure score by local authority")
    st.caption("Percentile-ranked blend of saturation (queuing, 0.6) + utilisation (0.4).")

    top = index.sort_values("pressure_score", ascending=False)
    st.dataframe(
        top[["pressure_rank", "local_authority", "pressure_score", "utilisation",
             "saturation_rate", "n_connectors", "n_chargepoints", "revenue_per_connector"]],
        use_container_width=True, hide_index=True
    )

    fig_bar = px.bar(top.head(20).sort_values("pressure_score"),
                     x="pressure_score", y="local_authority", orientation="h",
                     hover_data=["utilisation", "saturation_rate", "n_connectors"])
    fig_bar.update_layout(height=520, margin=dict(t=10))
    st.plotly_chart(fig_bar, use_container_width=True)

    st.subheader("Pressure map")
    mp = index.dropna(subset=["latitude", "longitude"])
    m_map = folium.Map(location=[56.6, -4.2], zoom_start=6, tiles="CartoDB positron")
    for _, r in mp.iterrows():
        folium.CircleMarker(
            [r["latitude"], r["longitude"]], radius=5 + 12 * r["pressure_score"],
            popup=(f"<b>{r['local_authority']}</b><br>Pressure {r['pressure_score']:.2f}<br>"
                   f"Utilisation {r['utilisation']:.1%}<br>Saturation {r['saturation_rate']:.1%}"),
            color="crimson", fill=True, fill_opacity=0.6
        ).add_to(m_map)
    st_folium(m_map, width=1100, height=520)

# ============================================================
# Tab 3 — Demand Archetypes
# ============================================================

with tab3:
    st.subheader("Charging archetypes")
    st.caption("Charge points clustered by behaviour (when used, how long, charger type).")

    centroids = clusters.groupby("archetype").agg(
        charge_points=("cp_id", "size"),
        median_duration_min=("median_duration_min", "mean"),
        rapid_share=("rapid_share", "mean"),
        **{c: (c, "mean") for c in TIME_SHARES}
    )
    st.dataframe(
        centroids[["charge_points", "median_duration_min", "rapid_share"]]
        .round(2).reset_index(),
        use_container_width=True, hide_index=True
    )

    # time-of-day signature per archetype
    sig = centroids[TIME_SHARES].reset_index().melt(
        id_vars="archetype", var_name="time", value_name="share")
    sig["time"] = sig["time"].str.replace("pct_", "")
    fig_sig = px.bar(sig, x="archetype", y="share", color="time", barmode="stack",
                     title="When each archetype is used")
    fig_sig.update_layout(height=380, xaxis_tickangle=-20, margin=dict(t=40))
    st.plotly_chart(fig_sig, use_container_width=True)

    st.subheader("Archetype mix by local authority")
    la = st.selectbox("Local authority", sorted(geo_clusters["local_authority"].unique()))
    la_mix = geo_clusters[geo_clusters["local_authority"] == la]["archetype"].value_counts()
    fig_la = px.bar(la_mix.reset_index(), x="archetype", y="count", title=f"{la} — charger archetypes")
    fig_la.update_layout(height=340, xaxis_tickangle=-20, margin=dict(t=40))
    st.plotly_chart(fig_la, use_container_width=True)

# ============================================================
# Tab 4 — Planning Priorities
# ============================================================

with tab4:
    st.subheader("Where — and what — to build")
    st.caption("High-pressure local authorities, with the dominant local archetype "
               "indicating the *type* of capacity to add.")

    pri = index.merge(dominant, on="local_authority", how="left")
    pri = pri.sort_values("pressure_score", ascending=False)

    def recommend(row):
        if row["pressure_score"] < 0.5:
            return "Monitor"
        kind = "rapid" if "Rapid" in str(row["dominant_archetype"]) else "AC (destination/long-stay)"
        return f"Expand — add {kind} capacity"

    pri["recommendation"] = pri.apply(recommend, axis=1)
    st.dataframe(
        pri[["pressure_rank", "local_authority", "pressure_score",
             "dominant_archetype", "n_connectors", "recommendation"]],
        use_container_width=True, hide_index=True
    )

# ============================================================
# Tab 5 — Data & Method
# ============================================================

with tab5:
    t = load_totals()
    st.subheader("Coverage")
    st.markdown(f"""
    - **Source:** ChargePlace Scotland public session data
    - **Period:** {t['date_min']} → {t['date_max']}
    - **Sessions:** {t['sessions']:,} across {t['chargers']:,} charge points
    """)
    st.subheader("Key caveats")
    st.markdown("""
    - **Network fragmentation:** CPS is handing chargers to other operators through
      2025–26, so the dataset *shrinks* over time. Falling totals ≠ falling demand
      (demand per charger is flat). **No demand forecast** is published for this reason.
    - **Pressure** = percentile-ranked saturation (0.6) + utilisation (0.4); absolute
      utilisation is low (~3–9%), so the ranking is *relative*.
    - **Archetypes** from k-means (k=6, silhouette 0.32) — soft boundaries, so treat
      archetypes as tendencies, not hard categories.
    """)
    st.subheader("Methodology")
    st.markdown("""
    `raw → clean → reference (charge point table via geocoding) → processed (index,
    clusters)`. See `docs/model-decisions.md` for the full reasoning.
    """)
