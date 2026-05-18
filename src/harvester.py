from utils import connect_to_warehouse, save_raw_snapshot
import pandas as pd
import logging


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)


organisation = "energy_systems_catapult"
datasets = "scotland_chargepoint_sessions"

catalog = connect_to_warehouse(organisation)
tables = catalog.list_tables(datasets)
table_name = tables[0] # HARD CODED: THERE IS ONLY ONE TABLE IN THIS DATASET

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

# is_anomaly filter comes from the original dataset from SENSE
scanner = table.scan(
    row_filter="is_anomaly = false",
    selected_fields=columns
)

frames = []

logger.info("Starting harvesting data...")

for i, batch in enumerate(scanner.to_arrow_batch_reader()):

    batch_df = batch.to_pandas()

    frames.append(batch_df)

    print(f"Processed batch {i + 1:,} | Rows: {len(batch_df):,}")

logger.info("Concatenating batches...")

df = pd.concat(frames, ignore_index=True)

logger.info(
    "Harvest complete | Total rows harvested: %s",
    f"{len(df):,}"
)

save_raw_snapshot(
    df=df,
    dataset_name="_table_".join(table_name),
    filters={
        "is_anomaly": False
    })