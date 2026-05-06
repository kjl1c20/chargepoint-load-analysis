# electricity-demand-forecasting

PyTorch-based model to forecast feeder-level electricity demand using UK smart meter data, and identified peak load risks to support grid planning decisions.

---

## Task definition

Answer the questions below in your own words. Treat this section as the “contract” for everything that follows: data, features, split, and metrics must all line up with what you write here.

### 1.1 Who is the prediction for?
This prediction is for DNO analysts. The goal is to support peak-load planning by forecasting demand spikes early enough to plan peak-shaving actions and battery storage strategy.


- **Target variable (column name)**: `total_consumption_active_import`
- **Unit of observation**: one substation × one feeder × one timestamp
- **Horizon**: 24 steps (same time tomorrow)

### 1.3 Granularity and scope

- **Geographic / entity scope**: per region
- **Time frequency**: half-hourly — one timestep is one `data_collection_log_timestamp` per feeder/substation row.

### 1.4 Success criteria (how you know it worked)

- **Primary metric**: MAE in kWh per timestep

### 1.5 Constraints and caveats *(honesty section)*

- **Data limits**: The exploration notebook currently uses 4 months of data in total. The current split uses 3 months for training and 1 month for testing. Full-table scans across all monthly tables may require chunked loading or higher-memory compute.