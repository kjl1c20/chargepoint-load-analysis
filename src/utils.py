import os
import requests
import pandas as pd
from datetime import datetime
import uuid
import json
import logging
from pathlib import Path
from dotenv import load_dotenv
from pyiceberg.catalog import load_catalog
import streamlit as st
from ydata_profiling import ProfileReport

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)


PROJECT_ROOT = Path.cwd()
load_dotenv(PROJECT_ROOT / ".env")

SENSE_CATALOG_URL = "https://catalog.sdr-sense.org.uk/api/catalog"
RAW_DATA_DIR = Path("./data/raw")
CLEAN_DATA_DIR = Path("./data/clean")
METADATA_DIR = Path("./data/metadata")
PROCESSED_DATA_DIR = Path("./data/processed")
EDA_DIR = Path("./data/eda")


def lookup_postcodes(postcodes: list[str]) -> dict[str, str]:
    """
    Returns a {postcode: admin_district} mapping via the postcodes.io outcode endpoint.
    Uses outward codes (e.g. AB10) so terminated postcodes resolve correctly.
    """
    unique = list(set(p for p in postcodes if p))
    unique_outcodes = list(set(p.split()[0] for p in unique))

    outcode_to_city = {}
    for outcode in unique_outcodes:
        try:
            resp = requests.get(
                f"https://api.postcodes.io/outcodes/{outcode}",
                timeout=10
            ).json()
            districts = (resp.get("result") or {}).get("admin_district") or []
            if districts:
                outcode_to_city[outcode] = districts[0]
            else:
                logger.warning("Outcode not resolved: %s", outcode)
        except requests.RequestException as e:
            logger.warning("Outcode lookup failed | %s: %s", outcode, e)

    results = {p: outcode_to_city[p.split()[0]] for p in unique if p.split()[0] in outcode_to_city}

    logger.info("Postcode lookup complete | %d/%d resolved", len(results), len(unique))
    return results


def connect_to_warehouse(warehouse_slug):
    """Connect to a specific SENSE organisation catalog."""
    client_id = os.environ.get("SENSE_CLIENT_ID")
    client_secret = os.environ.get("SENSE_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise ValueError(
            "Set SENSE_CLIENT_ID and SENSE_CLIENT_SECRET in .env "
            "(or export them in the environment)."
        )

    return load_catalog(
        "sense",
        **{
            "type": "rest",
            "uri": SENSE_CATALOG_URL,
            "credential": f"{client_id}:{client_secret}",
            "scope": "PRINCIPAL_ROLE:ALL",
            "warehouse": warehouse_slug,
        }
    )


def save_raw_snapshot(
    df: pd.DataFrame,
    dataset_name: str,
    filters: dict = None,
    limit = None,
    isminimal_eda = False
):
    """
    Save parquet snapshot + metadata manifest
    """

    # GENENRATE UNIQUE SNAPSHOT ID
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    short_uuid = str(uuid.uuid4())[:8]

    snapshot_id = f"{timestamp}_{short_uuid}"

    parquet_path = (
        RAW_DATA_DIR
        / f"{snapshot_id}.parquet"
    )

    metadata_path = (
        METADATA_DIR
        / f"{snapshot_id}.json"
    )

    eda_path = (
        EDA_DIR
        /f"{snapshot_id}.html"
    )

    # SAVE PARQUET
    df.to_parquet(parquet_path, index=False)

    # METADATA MANIFEST
    metadata = {
        "snapshot_id": snapshot_id,
        "dataset_name": dataset_name,
        "created_at": datetime.now().isoformat(),

        "date_range": {
            "start": df["start_time"].min().isoformat(),
            "end": df["start_time"].max().isoformat()
        },

        "data": {
            "rows": int(df.shape[0]),
            "columns": int(df.shape[1]),
            "column_names": list(df.columns),
            "duplicates": int(df.duplicated().sum()),
            "duplicates_percent": float(df.duplicated().sum()/int(df.shape[0]))
        },

        "filters": filters or {},

        "limit": limit or "No limit",

        "storage": {
            "format": "parquet",
            "path": str(parquet_path)
        }
    }

    # -------------------------------------------------
    # SAVE METADATA
    # -------------------------------------------------
    with open(metadata_path, "w") as f:
        json.dump(metadata, f, indent=4)

    logger.info("Snapshot successfully saved | Snapshot ID: %s", snapshot_id)

    # -------------------------------------------------
    # GENERATE EXPLORATORY DATA ANALYSIS REPORT
    # -------------------------------------------------
    ProfileReport(df, minimal=isminimal_eda).to_file(eda_path)
    logger.info("EDA Profile Report successfully saved | Snapshot ID: %s", snapshot_id)

    return snapshot_id


def save_clean_snapshot(df: pd.DataFrame, raw_snapshot_id: str, cleaning_report: dict = None):
    """Save cleaned parquet and cleaning report linked to its raw snapshot."""
    clean_path = CLEAN_DATA_DIR / f"{raw_snapshot_id}.parquet"
    report_path = METADATA_DIR / f"{raw_snapshot_id}_clean.json"

    df.to_parquet(clean_path, index=False)

    report = {
        "raw_snapshot_id": raw_snapshot_id,
        "created_at": datetime.now().isoformat(),
        **(cleaning_report or {})
    }

    with open(report_path, "w") as f:
        json.dump(report, f, indent=4)

    logger.info("Clean snapshot saved | Raw snapshot ID: %s", raw_snapshot_id)


def get_latest_snapshot_id():
    files = [f for f in Path(METADATA_DIR).glob("*.json") if not f.name.endswith("_clean.json")]

    latest_file = max(files, key=lambda f: f.stat().st_mtime)

    with open(latest_file, "r") as f:
        metadata = json.load(f)

    return metadata["snapshot_id"]


@st.cache_data
def load_data(data_type: str, snapshot_id: str) -> pd.DataFrame:
    """
    Loads a dataset (raw or processed) for a given snapshot_id.

    Parameters
    ----------
    data_type : str
        Type of data to load. Must be either 'raw' or 'processed'.
    snapshot_id : str
        Snapshot identifier used for versioned parquet files.

    Returns
    -------
    pd.DataFrame
        Loaded dataset as a pandas DataFrame.
    """

    DIR_MAP = {
        "raw": RAW_DATA_DIR,
        "clean": CLEAN_DATA_DIR,
        "processed": PROCESSED_DATA_DIR,
    }

    if data_type not in DIR_MAP:
        raise ValueError("data_type must be 'raw', 'clean', or 'processed'.")

    directory = DIR_MAP[data_type]
    
    if data_type == 'processed':
        file_path = directory / f"{snapshot_id}_result.parquet"
    else:
        file_path = directory / f"{snapshot_id}.parquet"

    if not file_path.exists():
        raise FileNotFoundError(f"{data_type.capitalize()} data not found: {file_path}")

    return pd.read_parquet(file_path)