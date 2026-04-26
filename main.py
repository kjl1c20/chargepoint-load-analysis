from utils import connect_to_warehouse

organisation = "energy_systems_catapult"
datasets = "ssen_aggregated_smart_meter_usage"

catalog = connect_to_warehouse(organisation)
tables = catalog.list_tables(datasets)
TABLE_PATH = "ssen_aggregated_smart_meter_usage.ssen_march2026"  

try:
    table = catalog.load_table(TABLE_PATH)
    print(f"✅ Loaded table: {TABLE_PATH}")
    df = table.scan(limit=100).to_pandas()
except Exception as e:
    print(f"❌ Could not load table: {e}")
    print("\nMake sure to update TABLE_PATH with a valid table from your available tables.")

print(df.head())