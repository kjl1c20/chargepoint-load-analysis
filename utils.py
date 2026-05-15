import os
import pandas as pd
from datetime import datetime
import uuid
import json
from pathlib import Path
from dotenv import load_dotenv
from pyiceberg.catalog import load_catalog

load_dotenv(Path(__file__).resolve().parent / ".env")

SENSE_CATALOG_URL = "https://catalog.sdr-sense.org.uk/api/catalog"
RAW_DATA_DIR = Path("./data/raw")
METADATA_DIR = Path("./data/metadata")

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
    limit = None
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

    # SAVE PARQUET
    df.to_parquet(parquet_path, index=False)

    # METADATA MANIFEST
    metadata = {
        "snapshot_id": snapshot_id,
        "dataset_name": dataset_name,
        "created_at": datetime.now().isoformat(),

        "data": {
            "rows": int(df.shape[0]),
            "columns": int(df.shape[1]),
            "column_names": list(df.columns)
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

    # -------------------------------------------------
    # LOGGING
    # -------------------------------------------------
    print("✅ Snapshot saved")
    print(f"📦 Snapshot ID: {snapshot_id}")
    print(f"📂 Data: {parquet_path}")
    print(f"📝 Metadata: {metadata_path}")

    return snapshot_id