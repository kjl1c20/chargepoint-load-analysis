"""
Scotland EV Charging — Infrastructure Planning Dashboard.

Single interactive page: a demand-pressure map of every charge point; click a point to
drill into that site's metrics and demand over time. Reads the Gold demand-pressure table
and Silver sessions from Databricks via the SQL connector (aggregations pushed to SQL).

Run:  poetry run streamlit run src/dashboard.py
Needs in .env:  DATABRICKS_SERVER_HOSTNAME (or DATABRICKS_HOST), DATABRICKS_HTTP_PATH, DATABRICKS_TOKEN
"""

import os

import pandas as pd
import plotly.express as px
import streamlit as st
from databricks import sql as dbsql
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(page_title="Scotland EV Charging — Planning", layout="wide")

GOLD_TABLE = os.getenv("GOLD_SITE_PRESSURE_TABLE", "chargepoint_analysis.gold.site_pressure")
SESSIONS_TABLE = os.getenv("SILVER_SESSIONS_TABLE", "chargepoint_analysis.silver.cps_sessions_clean")

# Scottish postcode areas → place name, for the region filter
AREA_NAMES = {
    "AB": "Aberdeen", "DD": "Dundee", "DG": "Dumfries", "EH": "Edinburgh",
    "FK": "Falkirk", "G": "Glasgow", "HS": "Outer Hebrides", "IV": "Inverness",
    "KA": "Kilmarnock", "KW": "Kirkwall", "KY": "Kirkcaldy", "ML": "Motherwell",
    "PA": "Paisley", "PH": "Perth", "TD": "Borders", "ZE": "Shetland",
}
ALL_REGIONS = "All Scotland"


# ============================================================
# Databricks SQL connection + cached query helper
# ============================================================

def _conn_params():
    host = (os.getenv("DATABRICKS_SERVER_HOSTNAME") or os.getenv("DATABRICKS_HOST") or "")
    host = host.replace("https://", "").replace("http://", "").rstrip("/")
    return host, os.getenv("DATABRICKS_HTTP_PATH"), os.getenv("DATABRICKS_TOKEN")


HOST, HTTP_PATH, TOKEN = _conn_params()
if not (HOST and HTTP_PATH and TOKEN):
    st.error(
        "Missing Databricks connection settings. Set DATABRICKS_SERVER_HOSTNAME "
        "(or DATABRICKS_HOST), DATABRICKS_HTTP_PATH and DATABRICKS_TOKEN in .env."
    )
    st.stop()


@st.cache_data(show_spinner="Querying Databricks…")
def run_query(query: str) -> pd.DataFrame:
    """Run a SQL query against the Databricks warehouse, return a pandas DataFrame."""
    with dbsql.connect(server_hostname=HOST, http_path=HTTP_PATH, access_token=TOKEN) as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            return cur.fetchall_arrow().to_pandas()


@st.cache_data
def load_sites():
    return run_query(f"SELECT * FROM {GOLD_TABLE}")


@st.cache_data
def load_totals():
    r = run_query(f"""
        SELECT count(*)                    AS sessions,
               count(DISTINCT cp_id)       AS chargers,
               sum(consumption_kwh) / 1000 AS energy_mwh,
               sum(amount)                 AS revenue,
               min(start_time)             AS date_min,
               max(start_time)             AS date_max
        FROM {SESSIONS_TABLE}
    """).iloc[0]
    return {
        "sessions": int(r["sessions"]),
        "chargers": int(r["chargers"]),
        "energy_mwh": float(r["energy_mwh"]),
        "revenue": float(r["revenue"]),
        "date_min": pd.to_datetime(r["date_min"]).date(),
        "date_max": pd.to_datetime(r["date_max"]).date(),
    }


@st.cache_data
def load_site_trend(cp_id: str):
    """Monthly sessions for one charge point — queried on demand when a site is clicked."""
    safe = cp_id.replace("'", "''")
    return run_query(f"""
        SELECT date_format(start_time, 'yyyy-MM') AS month, count(*) AS sessions
        FROM {SESSIONS_TABLE}
        WHERE cp_id = '{safe}'
        GROUP BY date_format(start_time, 'yyyy-MM')
        ORDER BY month
    """)


# ============================================================
# Page
# ============================================================

sites = load_sites()
totals = load_totals()

st.title("Scotland EV Charging Profile Analysis")
st.caption(
    f"ChargePlace Scotland public network · demand pressure by charge point · "
    f"{totals['date_min']} → {totals['date_max']}"
)

# ---- network KPI strip ----
k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Charge points", f"{totals['chargers']:,}")
k2.metric("Sessions", f"{totals['sessions']:,}")
k3.metric("Energy", f"{totals['energy_mwh']:,.0f} MWh")
k4.metric("Avg utilisation", f"{sites['utilisation'].mean():.1%}")
k5.metric("Revenue (context)", f"£{totals['revenue']:,.0f}")

st.divider()

# ---- map (left) + clicked-site detail (right) ----
st.subheader("Where is the network under pressure?")
st.caption(
    "Each point is a charge point, coloured by demand-pressure score "
    "(saturation 60% + utilisation 40%). **Click a point** to inspect that site."
)

map_col, detail_col = st.columns([3, 2], gap="large")

with map_col:
    areas = sorted(sites["postcode_area"].dropna().unique())
    region = st.selectbox(
        "Postcode area",
        [ALL_REGIONS] + areas,
        format_func=lambda a: a if a == ALL_REGIONS else f"{a} — {AREA_NAMES.get(a, a)}",
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
        custom_data=["cp_id", "pressure_rank", "postcode", "saturation_rate",
                     "utilisation", "n_connectors"],
        mapbox_style="open-street-map",
        zoom=zoom,
        center=center,
        height=640,
    )
    fig_map.update_traces(
        hovertemplate=(
            "<b>%{hovertext}</b> (Rank %{customdata[1]})<br>"
            "%{customdata[2]}<br>"
            "Pressure score: %{marker.color:.3f}<br>"
            "Saturation rate: %{customdata[3]:.1%}<br>"
            "Utilisation: %{customdata[4]:.1%}<br>"
            "Connectors: %{customdata[5]}"
            "<extra></extra>"
        )
    )
    fig_map.update_layout(margin=dict(t=0, b=0, l=0, r=0), coloraxis_colorbar=dict(title="Pressure"))

    event = st.plotly_chart(
        fig_map, use_container_width=True, on_select="rerun", key="pressure_map",
        selection_mode="points",
    )

    scope = ALL_REGIONS if region == ALL_REGIONS else f"{region} — {AREA_NAMES.get(region, region)}"
    st.caption(f"{len(mp):,} charge points shown ({scope}). Some ranked sites have no postcode area.")

# resolve the clicked charge point (cp_id carried in customdata[0])
selected_cp = None
points = (event or {}).get("selection", {}).get("points", [])
if points:
    cd = points[0].get("customdata")
    if cd:
        selected_cp = cd[0]

with detail_col:
    if selected_cp is None:
        st.info("👈 Click a charge point on the map to see its sessions, energy, "
                "utilisation, saturation and demand over time.")
    else:
        row = sites[sites["cp_id"] == selected_cp].iloc[0]

        st.markdown(f"### {row['site_name']}")
        loc = row["postcode"] if pd.notna(row["postcode"]) else "—"
        st.caption(f"{loc} · pressure rank #{int(row['pressure_rank'])} · "
                   f"score {row['pressure_score']:.3f} · {int(row['n_connectors'])} connector(s)")

        a1, a2 = st.columns(2)
        a1.metric("Total sessions", f"{int(row['total_sessions']):,}")
        a2.metric("Total energy", f"{row['total_energy_kwh'] / 1000:,.1f} MWh")
        b1, b2 = st.columns(2)
        b1.metric("Utilisation", f"{row['utilisation']:.1%}")
        b2.metric("Saturation rate", f"{row['saturation_rate']:.1%}")

        if bool(row["single_connector"]):
            st.info("Single-connector site: saturation equals utilisation by construction, "
                    "so its pressure score leans high.")

        trend = load_site_trend(selected_cp)
        fig_trend = px.area(trend, x="month", y="sessions", title="Demand over time")
        fig_trend.update_traces(line_color="#d73027", fillcolor="rgba(215,48,39,0.15)")
        fig_trend.update_layout(height=300, margin=dict(t=40, b=0, l=0, r=0),
                                xaxis_title="", yaxis_title="Sessions / month")
        st.plotly_chart(fig_trend, use_container_width=True)

st.divider()
st.caption("Data source: ChargePlace Scotland public session data · chargeplacescotland.org")
