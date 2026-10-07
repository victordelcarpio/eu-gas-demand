# European Gas Demand Pipeline

A clean, standalone Python pipeline for tracking European natural gas demand,
built from primary national TSO and market operator sources.

## Sources

| Country | Source | `source_type` | Lag |
|---------|--------|---------------|-----|
| Germany | Trading Hub Europe (THE) | `full_metered` | D-1 |
| France | GRTGaz | `full_metered` | D-3 to D-5 |
| UK | National Gas / Xoserve | `full_metered` | D-1 |
| Italy | Snam Rete Gas | `full_metered` | D-2 |
| Spain | Enagas (from 2023-01-01) | `full_metered` | D-2 |
| Czech Republic | OTE (V0 evaluation) | `full_metered` | D-3 |
| Denmark | Energi Data Service | `full_metered` | D-1 |
| Austria | AGGM | `full_metered` | D-2 |
| Lithuania | Amber Grid open data | `full_metered` | D-2 |
| Estonia | Elering dashboard | `full_metered` | D-2 |
| Finland | Gasgrid transparency Excel | `full_metered` | D-2 |
| Greece | DESFA validated off-takes | `full_metered` | D-3 |
| Portugal | REN Data Hub | `full_metered` | D-2 |
| Croatia | Plinacro SUKAP | `full_metered` | D-2 |
| Netherlands | ENTSOG / GTS Physical Flow | `transmission_allocation` | D-2 |
| Belgium, Poland, Hungary, Romania, Slovenia, Bulgaria | ENTSOG Physical Flow (DIS-/FNC- points) | `transmission_allocation` | D-2 |
| Latvia | ENTSOG Physical Flow (FNC-00205) | `transmission_allocation` | D-2 |
| Slovakia | ENTSOG aggregated balance + AGSI storage | `flow_derived` | D-2 |
| Finland (fallback) | ALSI LNG sendout (Inkoo + Hamina) + ENTSOG | `flow_derived` | D-2 |
| Sweden | Energinet Gasflow (DK→SE border) | `flow_derived` | D-2 |

### `source_type` values

| Value | Meaning |
|-------|---------|
| `full_metered` | National TSO reports fully metered system consumption |
| `transmission_allocation` | ENTSOG Physical Flow exit points (DIS-/FNC-); covers most but not all end-use |
| `flow_derived` | Mass balance (imports − exports ± storage) or LNG sendout proxy |

**Provisional flag:** Any data within the source's typical revision lag is marked
provisional. V0 OTE data for Czech Republic is always provisional until V1 is published
(16th of following month).

## Setup

### Requirements
- Python 3.11+
- Node.js 18+ (for PPTX generation)

### Install

```bash
# Python dependencies
pip install pandas requests numpy python-dateutil openpyxl xlrd

# Node dependency (PPTX only)
npm install pptxgenjs
```

## Usage

```bash
# Run with defaults (2025-01-01 to yesterday, output to ./output/)
python run.py

# Custom date range
python run.py --from 2024-01-01 --to 2024-12-31

# Dark background slide
python run.py --dark

# CSV only, skip PPTX
python run.py --csv-only

# More parallel workers (faster on good connection)
python run.py --workers 10
```

## Output

```
output/
├── eu_gas_demand_daily.csv    # daily rows: date, country, twh, bcm, source, provisional, method, source_type
├── eu_gas_demand_monthly.csv  # monthly aggregated
├── coverage_report.csv        # per-country coverage summary
└── eu_gas_demand.pptx         # two-chart slide
```

## Project structure

```
gas_demand/
├── extractors/
│   ├── base.py           # BaseExtractor class and schema
│   ├── entsog.py         # ENTSOG API client (direct + border flows)
│   ├── flow_derived.py   # Mass balance for SK, LV, LT, EE, FI, SE
│   └── national.py       # DE, FR, GB, IT, ES, CZ, DK, AT
├── pipeline.py           # Orchestrator — runs extractors in parallel
├── output.py             # CSV + PPTX builder
├── run.py                # CLI entry point
└── README.md
```

## Adding a new source

1. Create a class in `extractors/national.py` (or a new file) inheriting `BaseExtractor`
2. Set `country`, `source`, and optionally `method` and `source_type` (default: `"full_metered"`)
3. Implement `_fetch(from_date, to_date) -> pd.DataFrame`
   - Return DataFrame with columns: `date`, `twh`, and optionally `provisional`
   - The base class handles validation, schema enforcement, and provisional flagging
4. Register it in `pipeline.py` → `_build_extractors()`

## Notes on data availability in early October

Running in the first week of October, you can expect full September data for:
- DE, GB, DK, AT (D-1 to D-2 lag) — complete
- IT, ES, NL, BE, flow-derived countries — complete or missing last 1-2 days
- FR (GRTGaz) — possibly missing last 3-5 days of September
- CZ (OTE V0) — complete, but provisional until V1 published ~16 Oct

Any country missing tail days will appear with `days_missing > 0` in the
coverage report and be noted in the slide footnote. The missing days will be
picked up automatically on the next run.
