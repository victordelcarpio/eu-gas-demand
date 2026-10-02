# Data Sources

This document describes the data source used for each country in the pipeline: what is being measured, how it compares to Eurostat's official consumption figure, and any known limitations.

---

## Quick reference

| Country | Extractor type | Source | Granularity | Sector coverage | Own-use gap | History from | Lag |
|---------|---------------|--------|-------------|-----------------|-------------|--------------|-----|
| DE | National TSO API* | Trading Hub Europe (THE) | Daily | Total system (transmission) | ~3–5% | 2019 | D+1 |
| FR | National TSO API | ODRÉ / GRTGaz-TEREGA | Daily (half-hourly agg.) | Total system (trans. + dist.) | <1% | Jan 2012 | ~6 weeks (main); D+2 (sector fill) |
| GB | National TSO API* | National Gas / Xoserve | Daily | Total NTS demand | ~3–5% | — | D+1 |
| IT | ENTSOG allocation | Snam Rete Gas — 3 points | Daily | Distribution + industrial + power | ~4–8% | ~2010 | D+1 |
| ES | National TSO API | Enagas | Daily | Total national system | ~2–4% | ~2015 | D+2 |
| CZ | National market op. | OTE (V0/V1 evaluation) | Daily | Total system | ~3–5% | ~2015 | D+3 (V0); M+16d (V1) |
| DK | National TSO API | Energi Data Service (Energinet) | Daily | Transmission to distribution | ~20–28%† | ~2012 | D+1 |
| AT | National system op. | AGGM | Daily | Total system | ~3–5% | ~2019 | D+1 |
| EE | National TSO API | Elering Dashboard | Hourly (agg. to daily) | Total domestic flow from transmission | ~2–3% | ~2015 | D+1 |
| LT | National TSO API | Amber Grid open data | Daily | Distribution systems + directly connected | ~2–16% | Oct 2021 | D+1 |
| NL | ENTSOG aggregated | ENTSOG TP (broken) | — | — | — | — | — |
| BE | ENTSOG aggregated | ENTSOG TP | Daily | Aggregated exit (Fluxys) | ~3–6% | ~2012 | D+1 |
| PL | ENTSOG aggregated | ENTSOG TP | Daily | Aggregated exit (GAZ-SYSTEM) | ~3–6% | ~2012 | D+1 |
| HU | ENTSOG aggregated | ENTSOG TP | Daily | Aggregated exit (FGSZ) | ~3–6% | ~2012 | D+1 |
| RO | ENTSOG aggregated | ENTSOG TP | Daily | Aggregated exit (Transgaz) | ~3–6% | ~2012 | D+1 |
| GR | ENTSOG aggregated | ENTSOG TP | Daily | Aggregated exit (DESFA) | ~3–6% | ~2012 | D+1 |
| PT | ENTSOG aggregated | ENTSOG TP | Daily | Aggregated exit (REN) | ~3–6% | ~2012 | D+1 |
| HR | ENTSOG aggregated | ENTSOG TP | Daily | Aggregated exit (Plinacro) | ~3–6% | ~2012 | D+1 |
| SI | ENTSOG aggregated | ENTSOG TP | Daily | Aggregated exit (Plinovodi) | ~3–6% | ~2012 | D+1 |
| BG | ENTSOG aggregated | ENTSOG TP | Daily | Aggregated exit (Bulgartransgaz) | ~3–6% | ~2012 | D+1 |
| SK | Flow-derived | ENTSOG TP (balance) | Daily | Net imports (no domestic prod.) | ~5–10% | ~2012 | D+1 |
| LV | Flow-derived | ENTSOG TP (balance) | Daily | Net imports (no domestic prod.) | ~5–10% | ~2012 | D+1 |
| SE | Flow-derived | ENTSOG TP (balance) | Daily | Net imports (no domestic prod.) | ~5–10% | ~2012 | D+1 |
| FI | Flow-derived + LNG | ENTSOG TP + Gasgrid | Daily | Pipeline + LNG regasification | ~5–10% | ~2012 | D+1 |

\* Requires API token (see README)  
† See Denmark note below — structural undercount, not fixable from public data

---

## What "own-use gap" means

Every ENTSOG-based source reports **allocation** data: gas nominated and allocated to each exit point. This excludes gas used within the transmission system itself:

- **Compression fuel** — gas burned by compressor stations to move gas through pipelines (~1–3% of throughput)
- **Line-pack changes** — gas held as pressure buffer inside the pipes
- **Unaccounted-for gas** — meter uncertainty, leakage, small unmeasured offtakes

National TSO APIs that report **metered consumption** (FR via ODRÉ, ES via Enagas, EE via Elering, LT via Amber Grid) include these components and therefore show smaller gaps vs Eurostat IC_OBS. ENTSOG allocation-based sources systematically undercount by ~3–8%.

This is a structural limitation of the ENTSOG Transparency Platform data. It is not an extractor bug. When using pipeline figures for market reports, apply the following guidance:

- For FR, ES, EE, LT: data represents **total metered system demand**. Difference vs Eurostat is typically <5% and within normal revision range.
- For IT, and all ENTSOG-aggregated countries: data represents **transmission allocation only**. Expect a systematic ~4–8% undercount vs Eurostat IC_OBS.
- For flow-derived countries (SK, LV, SE, FI): data represents **net import balance**. Additional uncertainty from storage operator data and statistical differences.

---

## Country notes

### Germany (DE) — Trading Hub Europe

**What**: THE aggregates all gas balancing zones in Germany into a single national demand figure. The API reports MWh/h (hourly average), which we convert to TWh/day (× 24 ÷ 10⁶).

**Access**: As of mid-2025, THE moved consumption data behind an authenticated REST API. Requires a THE account and `THE_API_TOKEN` environment variable. Without it the extractor returns empty.

**Coverage**: Full national transmission system. Includes industrial, power generation, and distribution system offtakes at the transmission/distribution interface.

---

### France (FR) — ODRÉ / GRTGaz-TEREGA

**What**: Daily gross consumption across both French transmission operators (GRTGaz and TEREGA, merged into NaTranGroupe in 2025). The primary ODRÉ dataset `consommation-quotidienne-brute` is published at half-hourly resolution and aggregated to daily totals in MWh. When this dataset lags (typically the last 6–8 weeks), three sector datasets fill the gap: industrial, distribution network, and CCGT power generation.

**Coverage**: Total national gas system including distribution. Validated against Eurostat IC_OBS at mean −0.9% over 2022–2025 (42 months), max 2.1%. The small systematic undercount reflects minor non-reported offtakes (own-use + small unmetered).

**History**: From January 2012.

---

### United Kingdom (GB) — National Gas / Xoserve

**What**: NTS (National Transmission System) actual demand at D+1. The new download API (`data.nationalgas.com`) replaced the old MIP portal in 2025. Reports in GWh, converted to TWh.

**Access**: Requires OAuth token from `https://apideveloper.nationalgas.com/s/`. Set `NATIONAL_GAS_API_TOKEN` environment variable.

**Coverage**: Transmission system demand. UK gas day is 06:00–06:00 UTC.

---

### Italy (IT) — Snam Rete Gas via ENTSOG

**What**: Three ENTSOG aggregated allocation points, summed daily:
- `DIS-00005` — Snam delivery to Italian distribution networks (residential, small commercial, small industrial)
- `FNC-00005` — Large industrial consumers connected directly to the transmission system
- `FNC-00006` — Thermal (power generation) plants connected to the transmission system

**Coverage**: The three points together cover substantially all end-user demand on the Italian transmission system. The remaining 4–8% gap vs Eurostat IC_OBS is transmission own-use (compression fuel, losses) which is not reported on ENTSOG TP. This gap is **consistent and directional** — it is not a data error.

**Reliability**: ENTSOG TP is sometimes slow (~90s response times). The extractor retries each point up to 3 times with escalating timeouts (90/120/150s) to avoid silent data loss from timeouts.

**Note**: Snam's own Jarvis portal (which would give direct metered data) is authenticated and not publicly accessible.

---

### Spain (ES) — Enagas

**What**: Enagas publishes daily national system demand (Sistema Nacional) via their transparency portal. The API returns approximately 80 days of data per call (daily GWh, converted to TWh). We make overlapping calls to cover multi-year ranges.

**Coverage**: Total national gas system including distribution. Validated against Eurostat IC_OBS at mean −2.6% over 2023–2025. The undercount reflects transmission own-use.

**Reliability**: The Enagas API is intermittently slow. The extractor uses 60-day steps (with up to 14-day retry fallback per window), dict-based deduplication to prevent double-counting from overlapping windows, and 60s timeouts with retry on timeout.

---

### Czech Republic (CZ) — OTE

**What**: OTE (Czech gas and electricity market operator) publishes daily ZIP files with gas evaluation data. V0 evaluation is published D+3 (provisional); V1 is published the 16th of the following month (more final). The extractor tries V1 first, falls back to V0.

**Coverage**: Total national system consumption. Daily ZIP per gas day.

---

### Denmark (DK) — Energi Data Service (Energinet)

**What**: The Gasflow dataset from Energi Data Service reports daily gas flows through the Energinet transmission system. `KWhToDenmark` (negative convention, gas leaving transmission) is used as the consumption proxy.

**Known limitation — structural undercount (~20–28%)**: A significant share of Danish gas consumption comes from **biogas injected directly into distribution networks** (DSO level). This gas never passes through the Energinet transmission system and therefore does not appear in Gasflow. This is a systemic gap that cannot be closed from publicly available data — it would require aggregated DSO metering data which is not published. The pipeline figure represents transmission-delivered consumption only. For reports, note that Danish figures are transmission-only and understate total gas use by approximately 20–28%.

---

### Austria (AT) — AGGM

**What**: AGGM (Austrian Gas Grid Management) publishes daily consumption data via their transparency portal. Values in MWh, converted to TWh.

---

### Estonia (EE) — Elering Dashboard

**What**: Elering's Dashboard API (`/api/gas-system`) returns hourly total domestic gas flow from the Estonian transmission network in kWh. We aggregate hourly → daily. Requests are chunked to ≤1 year due to API limits.

**Coverage**: Total domestic flow from transmission to end users. Validated against Eurostat IC_OBS at approximately −2–3%.

**History**: Available from approximately 2015.

---

### Lithuania (LT) — Amber Grid

**What**: Amber Grid's open data portal provides daily totals of domestic consumption in kWh, combining:
- Gas transmitted to distribution systems
- Gas transmitted to directly connected (large industrial) consumers

The API is at `ambergrid.lt/en/lietuvos-suvartojimo-duomenu-skaiciuokle/755/search`. We request monthly chunks to stay within API limits.

**Coverage**: Validated against Eurostat IC_OBS at −2% to −16% across comparable months. The gap is transmission own-use and is consistent and directional. One outlier (May 2026: +93%) reflects provisional Eurostat data, not an extractor error.

**History**: Data available from October 2021. Returns empty for earlier dates.

---

### ENTSOG Transparency Platform (BE, PL, HU, RO, GR, PT, HR, SI, BG)

**What**: For these countries the pipeline uses ENTSOG's aggregated exit data (`/api/v1/aggregatedData`, indicator `Nomination` or `Allocation`). This gives daily kWh at the national TSO level.

**Coverage**: Transmission-level allocation only. See "own-use gap" section above for the systematic undercount.

**Note**: The ENTSOG aggregated endpoint is inconsistently implemented across operators. Netherlands (NL) is currently broken (returns empty). Countries where the endpoint works typically show 3–6% undercount vs Eurostat.

---

### Flow-derived (SK, LV, SE, FI)

**What**: For countries with little or no domestic production and where no national TSO API is available, consumption is estimated from the net import balance: `entry flows − exit flows` from ENTSOG TP point-level data.

**Accuracy**: Less reliable than direct metered or allocation data. Sensitive to data gaps at individual cross-border points, unaccounted-for storage movements, and line-pack changes. Error vs Eurostat IC_OBS is typically 5–15% but can be larger for individual months.

**Finland**: Uses a hybrid approach — ENTSOG pipeline flows plus LNG regasification from the Inkoo terminal.

---

## Eurostat benchmarks

The pipeline validates against two Eurostat series from the `nrg_cb_gasm` dataset:

- **IC_OBS** (`M.IC_OBS.G3000`) — Inland consumption, observed. Monthly, reported by national statistical offices. Unit: TJ_GCV, converted to TWh by dividing by 3,600. This is the primary benchmark.
- **IMP / EXP** — Used as an order-of-magnitude sanity check (net imports ≠ consumption due to domestic production and storage changes, but should be in the right order of magnitude).

Eurostat typically publishes final monthly data with a 2–3 month lag; the most recent months are marked provisional and may be revised.
