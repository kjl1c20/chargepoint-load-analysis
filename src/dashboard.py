import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import folium
from folium.plugins import HeatMap
from streamlit_folium import st_folium

st.set_page_config(
    page_title="Scotland Charge Point Load Analysis",
    layout="wide"
)


@st.cache_data
def load_data():

    df = pd.read_parquet(
        "./data/processed/20260515_190346_3400a811_result.parquet" # HARDCODED INPUT
    )

    return df


df = load_data()
df = df.rename(columns={"cluster": "city"})

st.title("Scotland Charge Point Load Analysis")

st.markdown("""
This dashboard identifies cities in Scotland that may require
additional EV charging infrastructure based on
charger utilisation and charging demand patterns.
""")


# ============================================================
# SIDEBAR FILTERS
# ============================================================

st.sidebar.header("Filters")

min_probability = st.sidebar.slider(
    "Infrastructure Pressure Threshold",
    min_value=0.0,
    max_value=1.0,
    value=0.5
)

top_n = st.sidebar.slider(
    "Top High-Risk Cities",
    min_value=5,
    max_value=50,
    value=10
)


# ============================================================
# FILTER DATA
# ============================================================

filtered_df = df[
    df["need_probability"] >= min_probability
]


# ============================================================
# KPI METRICS
# ============================================================

col1, col2, col3 = st.columns(3)

with col1:
    st.metric(
        "Cities Analysed",
        len(df)
    )

with col2:
    st.metric(
        "High-Risk Cities",
        len(filtered_df)
    )

with col3:
    st.metric(
        "Avg Need Probability",
        round(df["need_probability"].mean(), 2)
    )


# ============================================================
# TOP RISK Cities
# ============================================================

st.subheader("Highest Risk Cities")

top_cities = (
    filtered_df
    .sort_values(
        by="need_probability",
        ascending=False
    )
    .head(top_n)
)

st.dataframe(
    top_cities[
        [
            "city",
            "need_probability",
            "sessions_per_connector",
            "num_connectors",
            "total_sessions",
            "total_energy_kwh"
        ]
    ],
    use_container_width=True
)


# ============================================================
# BAR CHART
# ============================================================

st.subheader("Charger Need Probability")

fig = px.bar(
    top_cities.sort_values(
        by="need_probability"
    ),

    x="need_probability",
    y="city",

    orientation="h",

    hover_data=[
        "sessions_per_connector",
        "num_connectors",
        "total_sessions"
    ]
)

st.plotly_chart(
    fig,
    use_container_width=True
)


# ============================================================
# FEATURE IMPORTANCE
# ============================================================

st.subheader("Feature Importance")

feature_importance = pd.DataFrame({
    "feature": [
        "sessions_per_connector",
        "total_sessions",
        "total_energy_kwh",
        "avg_session_duration",
        "num_connectors"
    ],
    "importance": [
        0.41,
        0.23,
        0.17,
        0.11,
        0.08
    ]
})

fig_importance = px.bar(
    feature_importance.sort_values(
        by="importance"
    ),

    x="importance",
    y="feature",

    orientation="h"
)

st.plotly_chart(
    fig_importance,
    use_container_width=True
)


# ============================================================
# HEATMAP
# ============================================================

st.subheader("EV Charger Demand Heatmap")

# ------------------------------------------------------------
# PREPARE MAP DATA
# ------------------------------------------------------------

map_df = (
    filtered_df
    .groupby("city")
    .agg({
        "latitude": "mean",
        "longitude": "mean",
        "need_probability": "mean",
        "sessions_per_connector": "mean"
    })
    .reset_index()
)


# ------------------------------------------------------------
# CREATE BASE MAP
# ------------------------------------------------------------

m = folium.Map(
    location=[56.4907, -4.2026],  # HARD CODED Scotland center
    zoom_start=6,
    tiles="CartoDB positron"
)


# ------------------------------------------------------------
# HEATMAP DATA
# ------------------------------------------------------------

heat_data = [
    [
        row["latitude"],
        row["longitude"],
        row["need_probability"]
    ]
    for _, row in map_df.iterrows()
]


# ------------------------------------------------------------
# ADD HEATMAP
# ------------------------------------------------------------

HeatMap(
    heat_data,

    radius=25,
    blur=18,
    max_zoom=10
).add_to(m)


# ------------------------------------------------------------
# ADD MARKERS
# ------------------------------------------------------------

for _, row in map_df.iterrows():

    popup_text = f"""
    <b>City:</b> {row['city']}<br>
    <b>Need Probability:</b> {row['need_probability']:.2f}<br>
    <b>Sessions per Connector:</b> {row['sessions_per_connector']:.2f}
    """

    folium.CircleMarker(
        location=[
            row["latitude"],
            row["longitude"]
        ],

        radius=6,

        popup=popup_text,

        fill=True
    ).add_to(m)


# ------------------------------------------------------------
# DISPLAY MAP
# ------------------------------------------------------------

st_folium(
    m,
    width=1200,
    height=700
)


# ============================================================
# RAW DATA
# ============================================================

with st.expander("View Raw Prediction Data"):

    st.dataframe(
        filtered_df,
        use_container_width=True
    )