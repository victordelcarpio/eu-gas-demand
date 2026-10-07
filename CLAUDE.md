# CLAUDE.md — Gas Demand Pipeline

This file is read by Claude Code at the start of every session. It captures non-obvious decisions and constraints that are not derivable from reading the code.

## Project overview

Python pipeline that fetches daily natural gas demand for 24 European countries from primary TSO sources. Output: `twh` and `bcm` per country per day, Jan 2025 to yesterday by default.

Owner: victor.delcarpio@auroraer.com

## Key constants and conversions

- **BCM conversion**: 1 BCM = 10.564 TWh → `bcm = twh / 10.564` (this is the agreed factor, do not change without checking with user)
- **Default start date**: `2025-01-01` (safe common floor across all 24 sources)
- **AGSI API key**: stored in `.env` as `GIE_AGSI_API_KEY`. Loaded via `python-dotenv`. Never write API keys to files or chat. The `.env` file must stay in `.gitignore`.

## Architecture decisions (why things are the way they are)

### Slovakia uses `/aggregatedData`, not point-level flows
SK is a major transit hub. Point-level ITP border points (AT/CZ/HU) report identical entry=exit values for bidirectional transit, so the net border balance is ~0. The ENTSOG `/aggregatedData` endpoint correctly disaggregates directional flows. See `SlovakiaExtractor` in `extractors/flow_derived.py`.

### AGSI storage correction is required for SK
The ENTSOG aggregated net import includes gas that entered from neighbors and went straight into storage (POZAGAS + NAFTA). Without subtracting daily net injection via AGSI, summer consumption is overstated. `GIE_AGSI_API_KEY` must be set or SK values are unreliable in summer.

### UGS-* points are excluded from ENTSOG border balance
ENTSOG labels underground storage interconnection points as "Transmission" infrastructure type. They are filtered out by checking `pointKey.startswith("UGS-")`. Including them would double-count storage flows vs the AGSI correction.

### ALSI unit: GWh/day not TWh
AGSI injection/withdrawal values come in GWh/day. The code divides by 1000 to get TWh. Do not change this.

### Finland hybrid approach
`FinlandGasgridExtractor` (national.py) fetches the Gasgrid Excel file — complete months only, lags by ~1 week. `FinlandLNGExtractor` (flow_derived.py) covers the current incomplete month using ALSI LNG sendout. Both register as FI; `method_rank` deduplication keeps the direct source for dates where both exist.

### Sweden source_type is flow_derived
Sweden uses `KWhToSweden` from the Energinet Gasflow dataset — this is a border flow (DK→SE cross-border), not metered domestic consumption. `source_type = "flow_derived"` even though the data comes from Energinet's own system.

### NL source_type is transmission_allocation
Despite having its own `NetherlandsGTSExtractor`, GTS data comes from ENTSOG Physical Flow exit points (DIS/FNC), not a metered national total.

## Known data history floors

| Country | Earliest reliable data | Reason |
|---------|----------------------|--------|
| ES | 2023-01-01 | Enagas API returns `{"Bad call": ...}` for any anchor date before 2023 |
| CZ | 2025-01-01 | ENTSOG points DIS-00208/FNC-00215 not published before Jan 2025 |
| BG | 2021-10-01 | FNC-00207 has no data before Oct 2021 |
| LT | 2021-10-01 | Amber Grid data starts Oct 2021 |

Both ES and CZ have `history_from` guards in the extractor / pipeline registry. Do not remove them — removing would cause false "missing days" warnings in coverage reports.

## source_type taxonomy

Three values, set as class attributes on each extractor:

- `full_metered` — national TSO reports fully metered system consumption (default)
- `transmission_allocation` — ENTSOG Physical Flow exit points (DIS-/FNC-); covers most but not all end-use
- `flow_derived` — mass balance (imports − exports ± storage) or LNG sendout proxy

## Retry / rate limiting

- **ENTSOG**: 429 rate-limit retries with delays [15, 30, 60]s in `_query()` in `extractors/entsog.py`
- **ALSI**: timeout raised to 60s, retries [15, 30]s in `_fetch_alsi_sendout()` in `extractors/flow_derived.py`
- **Plinacro HR**: retries up to 3× on `ConnectionError` with 5s/10s backoff (server drops connections intermittently)
- **Pipeline parallelism**: `max_workers=6` in `concurrent.futures.ThreadPoolExecutor`

## Files to know

```
extractors/base.py        — BaseExtractor, SCHEMA, source_type default
extractors/entsog.py      — ENTSOG API client, border flows, Physical Flow extractor
extractors/flow_derived.py — SK (aggregated balance + AGSI), FI LNG, SE
extractors/national.py    — all national TSO extractors
pipeline.py               — orchestrator, dedup by method_rank, bcm column
output.py                 — CSV + PPTX builder
run.py                    — CLI (--from, --to, --dark, --csv-only, --workers)
docs/data_sources.md      — per-country source details and Eurostat benchmarks
docs/architecture.md      — pipeline design overview
```

## PR history

- PR #9  — initial 24-country pipeline
- PR #10 — broken extractor fixes (2026)
- PR #11 — SK rewrite (aggregated balance + AGSI), LV fix (FNC-00205), 429 retry
- PR #12 — bcm column, Jan 2025 default start, ES history guard, source_type column
- PR #13 — Plinacro retry, ALSI timeout/retry, CZ history_from guard
