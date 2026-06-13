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
SITE_PATH = Path("./data/processed/site_pressure.parquet")

DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Scottish postcode areas → place name, for the region filter
AREA_NAMES = {
    "AB": "Aberdeen", "DD": "Dundee", "DG": "Dumfries", "EH": "Edinburgh",
    "FK": "Falkirk", "G": "Glasgow", "HS": "Outer Hebrides", "IV": "Inverness",
    "KA": "Kilmarnock", "KW": "Kirkwall", "KY": "Kirkcaldy", "ML": "Motherwell",
    "PA": "Paisley", "PH": "Perth", "TD": "Borders", "ZE": "Shetland",
}
ALL_REGIONS = "All Scotland"

if not SITE_PATH.exists():
    st.error(f"Missing {SITE_PATH}. Run site_pressure.py first.")
    st.stop()


# ============================================================
# cached loaders / aggregations
# ============================================================

@st.cache_data
def load_sites():
    return pd.read_parquet(SITE_PATH)


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
def load_cp_months():
    """Monthly session counts per charge point — for the single-site trend."""
    df = pd.read_parquet(CLEAN_PATH, columns=["cp_id", "start_time"])
    df["month"] = pd.to_datetime(df["start_time"]).dt.to_period("M").astype(str)
    return df.groupby(["cp_id", "month"]).size().reset_index(name="sessions")


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


sites = load_sites()

st.title("Scotland EV Charging Profile Analysis")
st.caption("ChargePlace Scotland public network · demand pressure by charge point (site)")

tab1, tab2 = st.tabs(
    ["Overview", "Per-Site Performance"]
)

# ============================================================
# Tab 1 — Overview (pressure map of every ranked site)
# ============================================================

with tab1:
    st.subheader("Where is the network under pressure?")
    st.caption(
        "Ranked charge points, placed geographically and coloured by demand-pressure "
        "score (saturation 60% + utilisation 40%, ranked relative to other sites). "
        "Hotspots are individual sites, not whole council areas. Filter by postcode area "
        "to focus on one region."
    )

    # region filter by postcode area (G, EH, AB ...)
    areas = sorted(sites["postcode_area"].dropna().unique())
    region = st.selectbox(
        "Postcode area",
        [ALL_REGIONS] + areas,
        format_func=lambda a: a if a == ALL_REGIONS
        else f"{a} — {AREA_NAMES.get(a, a)}",
    )

    mp = sites.dropna(subset=["latitude", "longitude"])
    if region == ALL_REGIONS:
        center, zoom = {"lat": 56.8, "lon": -4.2}, 5.3
    else:
        mp = mp[mp["postcode_area"] == region]
        center = {"lat": mp["latitude"].mean(), "lon": mp["longitude"].mean()}
        zoom = 8.5

    fig_map = px.scatter_mapbox(
        mp,
        lat="latitude",
        lon="longitude",
        color="pressure_score",
        color_continuous_scale="OrRd",
        size="pressure_score",
        size_max=18,
        hover_name="site_name",
        custom_data=["pressure_rank", "utilisation", "saturation_rate",
                     "n_connectors", "local_authority"],
        mapbox_style="open-street-map",
        zoom=zoom,
        center=center,
        height=720,
    )
    fig_map.update_traces(
        hovertemplate=(
            "<b>%{hovertext}</b> (Rank %{customdata[0]})<br>"
            "%{customdata[4]}<br>"
            "Pressure score: %{marker.color:.3f}<br>"
            "Saturation rate: %{customdata[2]:.1%}<br>"
            "Utilisation: %{customdata[1]:.1%}<br>"
            "Connectors: %{customdata[3]}"
            "<extra></extra>"
        )
    )
    fig_map.update_layout(margin=dict(t=0, b=0, l=0, r=0))
    st.plotly_chart(fig_map, use_container_width=True)
    scope = ALL_REGIONS if region == ALL_REGIONS else f"{region} — {AREA_NAMES.get(region, region)}"
    st.caption(f"{len(mp):,} charge points shown ({scope}) · "
               "ungeocoded sites excluded; ~7% of ranked sites have no postcode area.")

# ============================================================
# Tab 2 — Per-Site Performance (network context + single-site drill-down)
# ============================================================

with tab2:
    # ---- network-level performance context ----
    t = load_totals()
    st.caption(f"Analysis Period: {t['date_min']} → {t['date_max']}")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Charge points", f"{t['chargers']:,}")
    c2.metric("Sessions", f"{t['sessions']:,}")
    c3.metric("Energy", f"{t['energy_mwh']:,.0f} MWh")
    c4.metric("Avg utilisation (ranked sites)", f"{sites['utilisation'].mean():.1%}")
    c5.metric("Revenue (context)", f"£{t['revenue']:,.0f}")

    st.subheader("Demand over time")
    st.caption("Total sessions fall as chargers migrate off the CPS network, but demand "
               "*per charger* stays flat — the network is fragmenting, not shrinking in use.")
    m = load_monthly()
    fig = go.Figure()
    fig.add_bar(x=m["month"], y=m["sessions"], name="Total sessions", marker_color="#9ecae1")
    fig.add_scatter(x=m["month"], y=m["active_chargers"], name="Active chargers",
                    yaxis="y2", line=dict(color="crimson"))
    fig.update_layout(
        yaxis=dict(title="Sessions"),
        yaxis2=dict(title="Active chargers", overlaying="y", side="right", showgrid=False),
        legend=dict(orientation="h"), height=360, margin=dict(t=10)
    )
    st.plotly_chart(fig, use_container_width=True)

    fig_pc = px.line(m, x="month", y="sessions_per_charger",
                     title="Sessions per charger (real per-charger demand — flat)")
    fig_pc.update_layout(height=280, margin=dict(t=40))
    st.plotly_chart(fig_pc, use_container_width=True)

    st.subheader("When is the network busiest?")
    hd = load_hour_dow()
    fig_h = px.imshow(hd.values, x=list(range(24)), y=DOW, aspect="auto",
                      color_continuous_scale="OrRd", labels=dict(x="Hour", y="", color="Sessions"))
    fig_h.update_layout(height=300, margin=dict(t=10))
    st.plotly_chart(fig_h, use_container_width=True)

    st.divider()

    # ---- single-site drill-down ----
    st.subheader("Inspect a single charge point")

    ranked = sites.sort_values("pressure_score", ascending=False).reset_index(drop=True)
    # cp_id is unique; site_name can repeat across charge points, so key the picker on cp_id.
    options = ranked["cp_id"].tolist()
    labels = {
        r.cp_id: f"#{r.pressure_rank} · {r.site_name} — {r.local_authority}"
        for r in ranked.itertuples()
    }
    pick = st.selectbox("Charge point (ranked by pressure)", options,
                        format_func=lambda c: labels[c])
    row = ranked[ranked["cp_id"] == pick].iloc[0]

    d1, d2, d3, d4 = st.columns(4)
    d1.metric("Pressure rank", f"#{int(row['pressure_rank'])}", f"score {row['pressure_score']:.3f}")
    d2.metric("Saturation rate", f"{row['saturation_rate']:.1%}")
    d3.metric("Utilisation", f"{row['utilisation']:.1%}")
    d4.metric("Connectors", f"{int(row['n_connectors'])}",
              "single — sat=util" if row["single_connector"] else None)
    if row["single_connector"]:
        st.info("Single-connector site: saturation equals utilisation by construction, so "
                "its pressure score leans high. Real pressure (no redundancy), but read it "
                "with that in mind.")

    cm = load_cp_months()
    site_m = cm[cm["cp_id"] == pick].sort_values("month")
    fig_sm = px.line(site_m, x="month", y="sessions", markers=True,
                     title="Sessions per month at this site")
    fig_sm.update_layout(height=320, margin=dict(t=40))
    st.plotly_chart(fig_sm, use_container_width=True)

    st.divider()
    st.subheader("Full ranking")
    st.caption("All ranked charge points. This ranks where to **expand existing strained "
               "sites** — it cannot see net-new demand where there are no chargers yet.")
    st.dataframe(
        ranked[["pressure_rank", "site_name", "local_authority", "pressure_score",
                "saturation_rate", "utilisation", "n_connectors", "single_connector"]],
        use_container_width=True, hide_index=True,
        column_config={
            "pressure_score": st.column_config.NumberColumn(format="%.3f"),
            "saturation_rate": st.column_config.NumberColumn(format="%.3f"),
            "utilisation": st.column_config.NumberColumn(format="%.3f"),
        },
    )

st.divider()
st.caption("Data source: ChargePlace Scotland public session data · chargeplacescotland.org")
