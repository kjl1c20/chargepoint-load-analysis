"""
Scotland EV Charging — Infrastructure Planning Dashboard.

Run:  poetry run streamlit run src/dashboard.py
"""

from pathlib import Path

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
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

tab1, tab2, tab3, tab4 = st.tabs(
    ["Overview", "Demand Pressure", "Demand Archetypes", "Planning Priorities"]
)

# ============================================================
# Tab 1 — Overview
# ============================================================

with tab1:
    t = load_totals()
    st.caption(f"Analysis Period: {t['date_min']} → {t['date_max']}")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Charge points", f"{t['chargers']:,}")
    c2.metric("Sessions", f"{t['sessions']:,}")
    c3.metric("Energy", f"{t['energy_mwh']:,.0f} MWh")
    c4.metric("Avg utilisation", f"{index['utilisation'].mean():.1%}")
    c5.metric("Revenue (context)", f"£{t['revenue']:,.0f}")

    st.subheader("Demand over time")
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
    st.caption(
        "Each local authority is scored by saturation rate (share of time all connectors are "
        "simultaneously busy, weight 60%) and utilisation (occupancy rate, weight 40%), "
        "both ranked relative to other authorities."
    )

    top = index.sort_values("pressure_score", ascending=False).reset_index(drop=True)
    top["rank_label"] = top["pressure_rank"].astype(str) + ". " + top["local_authority"]

    n_show = st.slider("Local authorities to show", min_value=5, max_value=len(top), value=len(top), step=1)
    display_df = top.head(n_show).sort_values("pressure_score")
    chart_h = max(400, n_show * 24)

    col_bar, col_map = st.columns([1, 1])

    with col_bar:
        fig_bar = px.bar(
            display_df,
            x="pressure_score",
            y="rank_label",
            orientation="h",
            color="pressure_score",
            color_continuous_scale="OrRd",
            custom_data=["local_authority", "pressure_rank", "utilisation",
                         "saturation_rate", "n_chargepoints", "n_connectors", "revenue_per_connector"],
        )
        fig_bar.update_traces(
            hovertemplate=(
                "<b>%{customdata[0]}</b> (Rank %{customdata[1]})<br>"
                "Pressure score: %{x:.3f}<br>"
                "Saturation rate: %{customdata[3]:.1%}<br>"
                "Utilisation: %{customdata[2]:.1%}<br>"
                "Charge points: %{customdata[4]:,} (%{customdata[5]:,} connectors)<br>"
                "Revenue / connector: £%{customdata[6]:,.0f}"
                "<extra></extra>"
            )
        )
        fig_bar.update_layout(
            height=chart_h,
            margin=dict(t=10, l=10),
            coloraxis_showscale=False,
            yaxis_title="",
            xaxis_title="Pressure score",
        )
        st.plotly_chart(fig_bar, use_container_width=True)

    with col_map:
        mp = display_df.dropna(subset=["latitude", "longitude"])
        fig_map = px.scatter_mapbox(
            mp,
            lat="latitude",
            lon="longitude",
            color="pressure_score",
            color_continuous_scale="OrRd",
            size="pressure_score",
            size_max=22,
            hover_name="local_authority",
            custom_data=["pressure_rank", "utilisation", "saturation_rate", "n_chargepoints"],
            mapbox_style="open-street-map",
            zoom=5,
            center={"lat": 56.6, "lon": -4.2},
            height=chart_h,
        )
        fig_map.update_traces(
            hovertemplate=(
                "<b>%{hovertext}</b> (Rank %{customdata[0]})<br>"
                "Pressure score: %{marker.color:.3f}<br>"
                "Saturation rate: %{customdata[2]:.1%}<br>"
                "Utilisation: %{customdata[1]:.1%}<br>"
                "Charge points: %{customdata[3]:,}"
                "<extra></extra>"
            )
        )
        fig_map.update_layout(margin=dict(t=10), coloraxis_showscale=False)
        st.plotly_chart(fig_map, use_container_width=True)

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
        a = str(row["dominant_archetype"])
        if "Rapid" in a:
            kind = "rapid chargers"
        elif "commuter" in a:
            kind = "AC workplace chargers"
        elif "depot" in a:
            kind = "AC long-stay / depot chargers"
        else:
            kind = "AC public / retail chargers"
        return f"Expand — add {kind}"

    pri["recommendation"] = pri.apply(recommend, axis=1)
    st.dataframe(
        pri[["pressure_rank", "local_authority", "pressure_score",
             "dominant_archetype", "n_connectors", "recommendation"]],
        use_container_width=True, hide_index=True
    )

st.divider()
st.caption("Data source: ChargePlace Scotland public session data · chargeplacescotland.org")
