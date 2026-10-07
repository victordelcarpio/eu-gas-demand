# Pipeline Architecture

## Overview

The pipeline fetches daily natural gas demand for 24 European countries from primary TSO/market-operator sources, merges them, and outputs daily CSV with TWh and BCM columns.

```
run.py  ──►  pipeline.run()  ──►  [extractor × 24]  ──►  output.py
                                    (parallel)
```

## Extractor pattern

Every extractor inherits `BaseExtractor` (`extractors/base.py`) and implements one method:

```python
def _fetch(self, from_date: date, to_date: date) -> pd.DataFrame:
    # Returns DataFrame with at minimum: date, twh
    # base class adds: country, source, method, source_type, provisional
```

`BaseExtractor.fetch()` (the public entry point) calls `_fetch()`, validates the schema, and marks provisional rows based on per-country lag constants.

### Class attributes

| Attribute | Type | Description |
|-----------|------|-------------|
| `country` | str | ISO2 country code |
| `source` | str | Human-readable label shown in reports |
| `method` | str | `"direct"` or `"flow_derived"` |
| `source_type` | str | `"full_metered"`, `"transmission_allocation"`, or `"flow_derived"` |

## Data flow

```
pipeline.run(from_date, to_date)
  │
  ├─ ThreadPoolExecutor(max_workers=6)
  │    ├─ extractor_1.fetch(from_date, to_date) → df
  │    ├─ extractor_2.fetch(from_date, to_date) → df
  │    └─ ... (24 extractors in parallel)
  │
  ├─ pd.concat(all_dfs)
  │
  ├─ Deduplication: sort by method_rank (direct=0, flow_derived=1)
  │    drop_duplicates(subset=["date","country"], keep="first")
  │    → national/direct source wins over flow_derived for same date+country
  │    (relevant for FI: Gasgrid Excel wins over ALSI for completed months)
  │
  ├─ Add bcm column: twh / 10.564
  │
  └─ Return (combined_df, coverage_report)
```

## Output schema

| Column | Type | Description |
|--------|------|-------------|
| `date` | date | Gas day |
| `country` | str | ISO2 |
| `twh` | float | Consumption in TWh |
| `bcm` | float | Consumption in BCM (twh / 10.564) |
| `source` | str | Source label |
| `provisional` | bool | True if within source lag window |
| `method` | str | `direct` or `flow_derived` |
| `source_type` | str | `full_metered`, `transmission_allocation`, or `flow_derived` |

## Extractor types

### National TSO APIs (`extractors/national.py`)
Direct HTTP calls to each country's own portal. Most return metered totals. Examples: DE (Trading Hub Europe), FR (ODRÉ/GRTGaz), IT (Snam via ENTSOG allocation), ES (Enagas), GR (DESFA Excel).

### ENTSOG Physical Flow (`extractors/entsog.py`)
Two-step process:
1. Query `/interconnections` to discover the TSO's exit point keys (DIS-xxxxx, FNC-xxxxx)
2. Query `/operationalData` per point key with `indicator=Physical Flow`, `directionKey=exit`

Used for: BE, PL, HU, RO, SI, BG, LV, NL, CZ.

Border flows (for flow-derived countries) use the same mechanism but query cross-border entry/exit points instead of domestic delivery points.

### Flow-derived (`extractors/flow_derived.py`)
Used where no direct metered or allocation point exists:

- **Slovakia (SK)**: `ENTSOG /aggregatedData` (country-level net import) + AGSI storage correction. Formula: `consumption = net_border_import + net_storage_withdrawal`
- **Finland fallback**: ALSI LNG sendout (Inkoo + Hamina terminals) + residual ENTSOG pipeline flows
- **Sweden**: `KWhToSweden` from Energinet Gasflow dataset (DK→SE cross-border flow ≈ consumption)

## API dependencies

| Service | URL | Auth | Used for |
|---------|-----|------|----------|
| ENTSOG TP | transparency.entsog.eu/api/v1 | None | 12+ countries |
| AGSI (GIE) | agsi.gie.eu/api | `x-key` header (`GIE_AGSI_API_KEY`) | SK storage correction |
| ALSI (GIE) | alsi.gie.eu/api | None | FI LNG sendout |
| Trading Hub Europe | api.tradinghub.eu | None | DE |
| ODRÉ | odre.org/api/explore | None | FR primary |
| National Gas | api.nationalgas.com | None | GB |
| Enagas | enagas.es | None | ES (2023+) |
| AGGM | aggm.at | None | AT |
| Energi Data Service | api.energidataservice.dk | None | DK, SE |
| Elering | dashboard.elering.ee | None | EE |
| Amber Grid | ambergrid.lt | None | LT |
| REN | servicebus.ren.pt | None | PT |
| Plinacro | sukap.plinacro.hr | None | HR |
| DESFA | desfa.gr | None | GR (Excel file) |
| Gasgrid | gasgrid.fi | None | FI (Excel file) |

## Adding a new country

1. Create a class in `extractors/national.py` (or new file) inheriting `BaseExtractor`
2. Set `country`, `source`, `method`, and `source_type`
3. Implement `_fetch(from_date, to_date) → pd.DataFrame` returning `[date, twh]` at minimum
4. Add to `_build_extractors()` in `pipeline.py`
5. Add provisional lag to `PROVISIONAL_LAG_DAYS` in `extractors/base.py`
6. Document in `docs/data_sources.md`

If data only starts from a certain date, either set `history_from` in the extractor or pass it as a constructor argument (see `ENTSOGPhysicalFlowExtractor` usage in `pipeline.py` for BG and CZ).
