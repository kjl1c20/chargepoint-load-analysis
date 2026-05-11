import os
from pathlib import Path

from dotenv import load_dotenv
from pyiceberg.catalog import load_catalog

load_dotenv(Path(__file__).resolve().parent / ".env")

SENSE_CATALOG_URL = "https://catalog.sdr-sense.org.uk/api/catalog"

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