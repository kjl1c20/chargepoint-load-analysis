from pyiceberg.catalog import load_catalog
# Replace these with your actual credentials
SENSE_CLIENT_ID = "dd75cc7d1cea8981"
SENSE_CLIENT_SECRET = "7ccd813c28ea32d45c6045a7df4901ea"

# The SENSE catalog URL
SENSE_CATALOG_URL = "https://catalog.sdr-sense.org.uk/api/catalog"

def connect_to_warehouse(warehouse_slug):
    """Connect to a specific SENSE organisation catalog."""
    return load_catalog(
        "sense",
        **{
            "type": "rest",
            "uri": SENSE_CATALOG_URL,
            "credential": f"{SENSE_CLIENT_ID}:{SENSE_CLIENT_SECRET}",
            "scope": "PRINCIPAL_ROLE:ALL",
            "warehouse": warehouse_slug,
        }
    )