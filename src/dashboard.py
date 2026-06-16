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
DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
# Session-length bands, ordered long → short so the heatmap shows long stays at the top.
DUR_BANDS = ["8h+", "4-8h", "2-4h", "1-2h", "30-60m", "<30m"]


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
def load_period():
    r = run_query(
        f"SELECT min(start_time) AS date_min, max(start_time) AS date_max FROM {SESSIONS_TABLE}"
    ).iloc[0]
    return pd.to_datetime(r["date_min"]).date(), pd.to_datetime(r["date_max"]).date()


def _in_clause(cp_ids: tuple) -> str:
    return ", ".join("'" + str(c).replace("'", "''") + "'" for c in cp_ids)


@st.cache_data
def load_site_profile(cp_ids: tuple):
    """Sessions by weekday × hour for a site's charge points — drives both daily charts."""
    return run_query(f"""
        SELECT weekday(start_time) AS dow, hour(start_time) AS hour, count(*) AS sessions
        FROM {SESSIONS_TABLE}
        WHERE cp_id IN ({_in_clause(cp_ids)})
        GROUP BY weekday(start_time), hour(start_time)
    """)


@st.cache_data
def load_site_daycounts(cp_ids: tuple):
    """Distinct weekday vs weekend dates for a site — to normalise the average profile."""
    return run_query(f"""
        SELECT CASE WHEN weekday(start_time) >= 5 THEN 'Weekend' ELSE 'Weekday' END AS day_type,
               count(DISTINCT to_date(start_time)) AS days
        FROM {SESSIONS_TABLE}
        WHERE cp_id IN ({_in_clause(cp_ids)})
        GROUP BY CASE WHEN weekday(start_time) >= 5 THEN 'Weekend' ELSE 'Weekday' END
    """)


@st.cache_data
def load_site_hour_duration(cp_ids: tuple):
    """Session counts by hour of day × session-length band — the combined timing/length heatmap."""
    band = """CASE
                WHEN duration_minutes < 30  THEN '<30m'
                WHEN duration_minutes < 60  THEN '30-60m'
                WHEN duration_minutes < 120 THEN '1-2h'
                WHEN duration_minutes < 240 THEN '2-4h'
                WHEN duration_minutes < 480 THEN '4-8h'
                ELSE '8h+'
              END"""
    return run_query(f"""
        SELECT hour(start_time) AS hour, {band} AS dur_band, count(*) AS sessions
        FROM {SESSIONS_TABLE}
        WHERE cp_id IN ({_in_clause(cp_ids)})
        GROUP BY hour(start_time), {band}
    """)


@st.cache_data
def load_site_trend(cp_ids: tuple):
    """Monthly sessions across all charge points at a site — queried when a site is clicked."""
    return run_query(f"""
        SELECT date_format(start_time, 'yyyy-MM') AS month, count(*) AS sessions
        FROM {SESSIONS_TABLE}
        WHERE cp_id IN ({_in_clause(cp_ids)})
        GROUP BY date_format(start_time, 'yyyy-MM')
        ORDER BY month
    """)


@st.cache_data
def build_site_view(cp: pd.DataFrame) -> pd.DataFrame:
    """Aggregate the per-cp_id gold table to one row per physical site (location).

    Charge points at the same coordinates are one site. Rates are recomputed from summed
    hours (not averaged), and pressure is re-ranked across sites.
    """
    g = (
        cp.groupby(["latitude", "longitude"], dropna=False)
        .agg(
            site_name=("site_name", "first"),
            postcode=("postcode", "first"),
            postcode_area=("postcode_area", "first"),
            n_charge_points=("cp_id", "nunique"),
            n_connectors=("n_connectors", "sum"),
            total_sessions=("total_sessions", "sum"),
            total_energy_kwh=("total_energy_kwh", "sum"),
            total_revenue=("total_revenue", "sum"),
            occupied_hours=("occupied_hours", "sum"),
            available_connector_hours=("available_connector_hours", "sum"),
            saturated_hours=("saturated_hours", "sum"),
            cp_available_hours=("cp_available_hours", "sum"),
            cp_ids=("cp_id", lambda s: tuple(s)),
        )
        .reset_index()
    )
    denom_u = g["available_connector_hours"].where(g["available_connector_hours"] > 0)
    g["utilisation"] = (g["occupied_hours"] / denom_u).fillna(0).clip(upper=1.0)
    denom_s = g["cp_available_hours"].where(g["cp_available_hours"] > 0)
    g["saturation_rate"] = (g["saturated_hours"] / denom_s).fillna(0)
    g["pressure_score"] = (
        0.6 * g["saturation_rate"].rank(pct=True) + 0.4 * g["utilisation"].rank(pct=True)
    )
    g["pressure_rank"] = g["pressure_score"].rank(ascending=False, method="min").astype(int)
    g["site_key"] = g["latitude"].round(6).astype(str) + "," + g["longitude"].round(6).astype(str)
    return g


# ============================================================
# Page
# ============================================================

def render_detail(row):
    """The clicked-site card: site-level metrics + demand-over-time."""
    if row is None:
        st.info("👈 Click a site on the map to inspect its sessions, energy, revenue, "
                "utilisation, saturation and demand over time.")
        return

    name = row["site_name"] if pd.notna(row["site_name"]) else "Unnamed site"
    st.markdown(f"### {name}")
    loc = row["postcode"] if pd.notna(row["postcode"]) else "—"
    st.caption(f"{loc} · pressure rank #{int(row['pressure_rank'])} · "
               f"score {row['pressure_score']:.3f} · "
               f"{int(row['n_charge_points'])} charge point(s) · {int(row['n_connectors'])} connectors")

    a1, a2, a3 = st.columns(3)
    a1.metric("Total sessions", f"{int(row['total_sessions']):,}")
    a2.metric("Total energy", f"{row['total_energy_kwh'] / 1000:,.1f} MWh")
    a3.metric("Total revenue", f"£{row['total_revenue']:,.0f}")
    b1, b2 = st.columns(2)
    b1.metric("Utilisation", f"{row['utilisation']:.1%}")
    b2.metric("Saturation rate", f"{row['saturation_rate']:.1%}")

    trend = load_site_trend(row["cp_ids"])
    fig_trend = px.area(trend, x="month", y="sessions", title="Demand over time")
    fig_trend.update_traces(line_color="#d73027", fillcolor="rgba(215,48,39,0.15)")
    fig_trend.update_layout(height=290, margin=dict(t=40, b=0, l=0, r=0),
                            xaxis_title="", yaxis_title="Sessions / month")
    st.plotly_chart(fig_trend, use_container_width=True)


def render_site_profiles(row):
    """Per-site charging profile: hour×day heatmap, hour×duration heatmap, weekday vs weekend."""
    if row is None:
        return
    cp_ids = row["cp_ids"]
    name = row["site_name"] if pd.notna(row["site_name"]) else "this site"
    st.markdown(f"#### When does **{name}** get used?")

    prof = load_site_profile(cp_ids)
    p1, p2 = st.columns(2, gap="large")

    with p1:
        st.caption("Sessions by hour and day of week")
        mat = (prof.pivot(index="dow", columns="hour", values="sessions")
               .reindex(range(7)).reindex(columns=range(24)).fillna(0))
        fig_h = px.imshow(
            mat.values, x=list(range(24)), y=DOW, aspect="auto",
            color_continuous_scale="OrRd", labels=dict(x="Hour of day", y="", color="Sessions"),
        )
        fig_h.update_layout(height=300, margin=dict(t=10, b=0, l=0, r=0))
        st.plotly_chart(fig_h, use_container_width=True)

    with p2:
        st.caption("When do long vs short sessions happen?")
        hd = load_site_hour_duration(cp_ids)
        mat2 = (hd.pivot(index="dur_band", columns="hour", values="sessions")
                .reindex(DUR_BANDS).reindex(columns=range(24)).fillna(0))
        fig_hd = px.imshow(
            mat2.values, x=list(range(24)), y=DUR_BANDS, aspect="auto",
            color_continuous_scale="OrRd",
            labels=dict(x="Hour of day", y="Session length", color="Sessions"),
        )
        fig_hd.update_layout(height=300, margin=dict(t=10, b=0, l=0, r=0))
        st.plotly_chart(fig_hd, use_container_width=True)

    st.caption("Average charging profile — weekday vs weekend")
    agg = prof.copy()
    agg["day_type"] = agg["dow"].apply(lambda d: "Weekend" if d >= 5 else "Weekday")
    agg = agg.groupby(["day_type", "hour"], as_index=False)["sessions"].sum()
    days = load_site_daycounts(cp_ids).set_index("day_type")["days"].to_dict()
    agg["avg_sessions"] = agg.apply(lambda r: r["sessions"] / days.get(r["day_type"], 1), axis=1)
    fig_w = px.line(
        agg, x="hour", y="avg_sessions", color="day_type", markers=True,
        color_discrete_map={"Weekday": "#4575b4", "Weekend": "#d73027"},
    )
    fig_w.update_layout(
        height=280, margin=dict(t=10, b=0, l=0, r=0),
        xaxis_title="Hour of day", yaxis_title="Avg sessions / day", legend_title="",
        legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="right", x=1.0),
    )
    st.plotly_chart(fig_w, use_container_width=True)


@st.fragment
def map_and_detail():
    """Interactive map + detail, isolated in a fragment so a click reruns only this block."""
    map_col, detail_col = st.columns([3, 2], gap="large")

    with map_col:
        areas = sorted(site_view["postcode_area"].dropna().unique())
        region = st.selectbox(
            "Postcode area",
            [ALL_REGIONS] + areas,
            format_func=lambda a: a if a == ALL_REGIONS else f"{a} — {AREA_NAMES.get(a, a)}",
        )

        mp = site_view.dropna(subset=["latitude", "longitude"])
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
            custom_data=["site_key", "pressure_rank", "postcode", "saturation_rate",
                         "utilisation", "n_connectors", "n_charge_points"],
            mapbox_style="carto-positron",
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
                "%{customdata[6]} charge point(s) · %{customdata[5]} connectors"
                "<extra></extra>"
            )
        )
        # Preserve the user's zoom/pan across reruns (e.g. after a point click) — but let
        # it re-centre when the region filter changes, by keying uirevision to `region`.
        fig_map.update_layout(margin=dict(t=0, b=0, l=0, r=0),
                              coloraxis_colorbar=dict(title="Pressure"),
                              uirevision=region)

        event = st.plotly_chart(
            fig_map, use_container_width=True, on_select="rerun", key="pressure_map",
            selection_mode="points",
        )

        scope = ALL_REGIONS if region == ALL_REGIONS else f"{region} — {AREA_NAMES.get(region, region)}"
        st.caption(f"{len(mp):,} sites shown ({scope}). Some sites have no postcode area.")

    # resolve the clicked site (site_key carried in customdata[0])
    site_row = None
    points = (event or {}).get("selection", {}).get("points", [])
    if points:
        cd = points[0].get("customdata")
        if cd:
            match = site_view[site_view["site_key"] == cd[0]]
            if not match.empty:
                site_row = match.iloc[0]

    with detail_col:
        with st.container(border=True):
            render_detail(site_row)

    if site_row is not None:
        st.markdown("")
        render_site_profiles(site_row)


sites = load_sites()
site_view = build_site_view(sites)
date_min, date_max = load_period()

st.title("Scotland EV Charging Profile Analysis")
st.caption(
    f"ChargePlace Scotland public network · demand pressure by site · "
    f"{date_min} → {date_max}"
)

st.subheader("Where is the network under pressure?")
st.caption(
    "Each point is a **site** (the charge points at one location), coloured by demand-pressure "
    "score (saturation 60% + utilisation 40%). **Click a site** to inspect it."
)

map_and_detail()

st.divider()
st.caption("Data source: ChargePlace Scotland public session data · chargeplacescotland.org")
