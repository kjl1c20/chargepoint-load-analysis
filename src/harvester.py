from utils import connect_to_warehouse, save_raw_snapshot
import pandas as pd
import matplotlib.pyplot as plt

organisation = "energy_systems_catapult"
datasets = "scotland_chargepoint_sessions"

catalog = connect_to_warehouse(organisation)
tables = catalog.list_tables(datasets)
table_name = tables[0]

rows_per_table = 100000
frames = []

table = catalog.load_table(table_name)

columns = [
    "connector_id",
    "cp_id",
    "connector_type",
    "duration",
    "consumption_kwh",
    "site",
    "start_time",
    "end_time",
    "City",
    "Postcode",
    "PricePerKWh",
    "Power_kW",
    "latitude",
    "longitude"
]

# is_anomaly filter comes with the datasets
df = (
    table.scan(row_filter="is_anomaly = false", selected_fields=columns, limit=rows_per_table)
         .to_pandas()
)

save_raw_snapshot(
        df=df,
        dataset_name="_table_".join(table_name),
        filters={
            "is_anomaly": False
        },
        limit=rows_per_table
    )