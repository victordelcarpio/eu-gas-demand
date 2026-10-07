"""
National TSO / market operator extractors for direct consumption data.

Germany  : Trading Hub Europe (THE) — authenticated REST API (token required)
France   : ODRÉ open data (RTE/GRTGaz/TEREGA) — public API
UK       : National Gas data portal — authenticated API (token required)
Italy    : Snam / ENTSOG — ENTSOG distribution exit point DIS-00005
Spain    : Enagas — JSON API (historico)
Czech    : OTE (gas market operator) — daily ZIP (V0 evaluation)
Denmark  : Energi Data Service — API (JSON)
Austria  : AGGM via WIFO CSV
"""

from datetime import date, timedelta
from io import BytesIO, StringIO
import concurrent.futures
import time
import zipfile
import pandas as pd
import requests
import logging
from .base import BaseExtractor

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; gas-demand-pipeline/1.0; "
        "research use; contact: your@email.com)"
    )
}


# ── Germany — Trading Hub Europe ─────────────────────────────────────────────

class GermanyTHEExtractor(BaseExtractor):
    """
    Trading Hub Europe (THE) — daily aggregated consumption data.

    As of mid-2025, THE moved consumption data behind an authenticated REST API
    (https://api.tradinghub.eu/api/dataexport/..., requires THE account credentials).
    The old static CSV files at tradinghub.eu/Portals/0/Verbrauch/ are gone (HTTP 404).

    Set env var THE_API_TOKEN to enable. Without it this extractor returns empty.
    """
    country = "DE"
    source  = "Trading Hub Europe (THE)"

    API_BASE = "https://api.tradinghub.eu/api/dataexport"

    def _fetch(self, from_date: date, to_date: date) -> pd.DataFrame:
        import os
        token = os.environ.get("THE_API_TOKEN", "").strip()
        if not token:
            logger.warning(
                "GermanyTHEExtractor: THE_API_TOKEN not set. "
                "The THE data portal now requires API authentication. "
                "Set THE_API_TOKEN to your API token and retry."
            )
            return pd.DataFrame()

        years = range(from_date.year, to_date.year + 1)
        dfs = []
        auth_headers = {**HEADERS, "Authorization": f"Bearer {token}"}
        for year in years:
            url = f"{self.API_BASE}/aggregatedConsumption"
            params = {
                "year":   year,
                "format": "csv",
            }
            try:
                r = requests.get(url, params=params, headers=auth_headers, timeout=30)
                r.raise_for_status()
                raw = r.content.decode("utf-8-sig", errors="replace")
                df  = pd.read_csv(StringIO(raw), sep=";", decimal=",")
                dfs.append(df)
            except Exception as e:
                logger.warning(f"THE API {year}: {e}")

        if not dfs:
            return pd.DataFrame()

        df = pd.concat(dfs, ignore_index=True)
        df.columns = [c.strip().lower() for c in df.columns]

        date_col  = next((c for c in df.columns if "datum" in c or "date" in c), None)
        value_col = next((c for c in df.columns if "verbrauch" in c or "consumption" in c), None)

        if not date_col or not value_col:
            raise ValueError(f"Unexpected THE columns: {df.columns.tolist()}")

        df = df[[date_col, value_col]].rename(columns={date_col: "date", value_col: "twh_raw"})
        df["date"]    = pd.to_datetime(df["date"], dayfirst=True, errors="coerce").dt.date
        df["twh_raw"] = pd.to_numeric(df["twh_raw"], errors="coerce")
        # THE value is in MWh/h (hourly average) — ×24 → MWh/day, ÷1e6 → TWh
        df["twh"] = df["twh_raw"] * 24 / 1_000_000

        df = df[(df["date"] >= from_date) & (df["date"] <= to_date)]
        return df.groupby("date", as_index=False)["twh"].sum()


# ── Germany — Eurostat monthly fallback ──────────────────────────────────────

class GermanyEurostatExtractor(BaseExtractor):
    """
    Monthly German gas demand from Eurostat nrg_cb_gasm (IC_OBS, GEO=DE).
    Unit TJ_GCV → TWh (÷3600). Monthly total distributed uniformly across days.

    Used as fallback when THE_API_TOKEN is not set. Lag ~2 months; data is
    the definitive Eurostat figure so accuracy vs IC_OBS is exact by definition.
    Most recent 3 months marked provisional (Eurostat revises early estimates).
    """
    country     = "DE"
    source      = "Eurostat nrg_cb_gasm (monthly, distributed)"
    method      = "direct"

    API_URL = (
        "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/nrg_cb_gasm"
    )

    def _fetch(self, from_date: date, to_date: date) -> pd.DataFrame:
        import calendar
        params = {
            "format": "JSON", "lang": "EN",
            "freq": "M", "unit": "TJ_GCV",
            "nrg_bal": "IC_OBS", "geo": "DE",
            "sinceTimePeriod": f"{from_date.year - 1}-01",
        }
        try:
            r = requests.get(self.API_URL, params=params, headers=HEADERS, timeout=60)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            logger.warning(f"Eurostat DE: {e}")
            return pd.DataFrame()

        dims = data.get("dimension", {})
        time_idx = {v: k for k, v in dims.get("time", {}).get("category", {}).get("index", {}).items()}
        values = data.get("value", {})

        monthly = {}
        for str_idx, val in values.items():
            period = time_idx.get(int(str_idx))
            if period:
                monthly[period] = float(val) / 3600  # TJ_GCV → TWh

        if not monthly:
            return pd.DataFrame()

        # Determine provisional cutoff: last 3 published months are provisional
        sorted_periods = sorted(monthly)
        provisional_set = set(sorted_periods[-3:])

        records = []
        for period, monthly_twh in monthly.items():
            yr, mo = int(period[:4]), int(period[5:7])
            days_in_month = calendar.monthrange(yr, mo)[1]
            daily_twh = monthly_twh / days_in_month
            for day in range(1, days_in_month + 1):
                d = date(yr, mo, day)
                if from_date <= d <= to_date:
                    records.append({
                        "date":        d,
                        "twh":         daily_twh,
                        "provisional": period in provisional_set,
                    })

        if not records:
            return pd.DataFrame()

        df = pd.DataFrame(records)
        return df.sort_values("date").reset_index(drop=True)


# ── France — ODRÉ (RTE/GRTGaz/TEREGA open data) ──────────────────────────────

class FranceGRTGazExtractor(BaseExtractor):
    """
    France daily gas consumption via the ODRÉ open data platform.

    Primary source: `consommation-quotidienne-brute` (half-hourly gross consumption
    across GRTGaz + TEREGA networks, aggregated to daily TWh). Covers Jan 2012,
    lags ~6 weeks.

    When the primary dataset's coverage ends before to_date (typically the most
    recent 6-8 weeks), three sector datasets fill the gap automatically:
      - Industrial:    conso-journa-industriel-grtgazterega
      - Distribution:  courbe-de-charge-eldgrd-regional-grtgaz-terega
      - CCGT:          conso-horaire-cccg-nat
    These together reconstruct total national demand with a lag of ~1-2 days.

    GRTGaz was renamed NaTranGroupe in 2025 after merging with TEREGA.
    """
    country = "FR"
    source  = "ODRÉ / GRTGaz-TEREGA"

    _ODRE_BASE = (
        "https://odre.opendatasoft.com/api/explore/v2.1/catalog/datasets"
    )
    ODRE_URL = _ODRE_BASE + "/consommation-quotidienne-brute/records"

    # (dataset_id, field_name) — daily MWh values, summed per date
    _SECTOR_DATASETS = [
        ("conso-journa-industriel-grtgazterega",
         "consommation_journaliere_mwh_pcs"),
        ("courbe-de-charge-eldgrd-regional-grtgaz-terega",
         "conso_journaliere_mwh_pcs_0degc"),
        ("conso-horaire-cccg-nat",
         "conso_journaliere_mwh_pcs_0degc"),
    ]

    def _fetch(self, from_date: date, to_date: date) -> pd.DataFrame:
        main_df = self._fetch_main(from_date, to_date)

        last_main = main_df["date"].max() if not main_df.empty else None
        gap_from  = (last_main + timedelta(days=1)) if last_main else from_date

        if gap_from <= to_date:
            sector_df = self._fetch_sectors(gap_from, to_date)
            if not sector_df.empty:
                main_df = pd.concat([main_df, sector_df], ignore_index=True)

        if main_df.empty:
            return pd.DataFrame()
        return main_df.groupby("date", as_index=False)["twh"].sum()

    def _fetch_main(self, from_date: date, to_date: date) -> pd.DataFrame:
        buf = timedelta(days=2)
        ts_from = (from_date - buf).isoformat() + "T22:00:00"
        ts_to   = (to_date  + buf).isoformat() + "T22:00:00"
        day_count = (to_date - from_date).days + 1

        params = {
            "where":    (
                f'date_heure >= "{ts_from}" AND date_heure < "{ts_to}"'
                ' AND consommation_brute_gaz_totale IS NOT NULL'
            ),
            "group_by": "date",
            "select":   "date, SUM(consommation_brute_gaz_totale) as total_mwh",
            "order_by": "date",
            "limit":    min(day_count + 10, 10000),
        }
        try:
            r = requests.get(self.ODRE_URL, params=params, headers=HEADERS, timeout=60)
            r.raise_for_status()
            rows = r.json().get("results", [])
        except Exception as e:
            logger.warning(f"ODRÉ France main: {e}")
            return pd.DataFrame()

        records = []
        for row in rows:
            raw_date = row.get("date", "")    # "DD/MM/YYYY"
            mwh = row.get("total_mwh")
            if not raw_date or mwh is None:
                continue
            try:
                d = date(int(raw_date[6:10]), int(raw_date[3:5]), int(raw_date[0:2]))
                records.append({"date": d, "twh": float(mwh) / 1_000_000})
            except (ValueError, IndexError):
                continue

        if not records:
            return pd.DataFrame()
        df = pd.DataFrame(records)
        return df[(df["date"] >= from_date) & (df["date"] <= to_date)].copy()

    def _fetch_sectors(self, from_date: date, to_date: date) -> pd.DataFrame:
        """Combine industrial + distribution + CCGT sector datasets for recent dates.

        Only returns dates where all three sectors have data; partial-sector days
        (e.g., September when only CCGT is published) are excluded to avoid
        severe undercounting.
        """
        day_count = (to_date - from_date).days + 1
        date_totals: dict = {}
        sector_dates: list[set] = []

        for dataset_id, field in self._SECTOR_DATASETS:
            url = f"{self._ODRE_BASE}/{dataset_id}/records"
            params = {
                "where":    (
                    f"date >= date'{from_date.isoformat()}'"
                    f" AND date <= date'{to_date.isoformat()}'"
                ),
                "group_by": "date",
                "select":   f"date, SUM({field}) as total_mwh",
                "order_by": "date",
                "limit":    min(day_count + 10, 10000),
            }
            covered: set = set()
            try:
                r = requests.get(url, params=params, headers=HEADERS, timeout=60)
                r.raise_for_status()
                for row in r.json().get("results", []):
                    raw_date = row.get("date", "")   # "YYYY-MM-DD"
                    mwh = row.get("total_mwh")
                    if not raw_date or mwh is None:
                        continue
                    try:
                        d = date.fromisoformat(raw_date[:10])
                        date_totals[d] = date_totals.get(d, 0.0) + float(mwh)
                        covered.add(d)
                    except (ValueError, TypeError):
                        continue
            except Exception as e:
                logger.warning(f"ODRÉ sector {dataset_id}: {e}")
            sector_dates.append(covered)

        if not date_totals or not all(sector_dates):
            return pd.DataFrame()

        # Only keep dates covered by every sector to avoid undercounting
        all_covered = set.intersection(*sector_dates)

        records = [
            {"date": d, "twh": mwh / 1_000_000}
            for d, mwh in sorted(date_totals.items())
            if from_date <= d <= to_date and d in all_covered
        ]
        if not records:
            return pd.DataFrame()
        return pd.DataFrame(records)


# ── UK — National Gas ─────────────────────────────────────────────────────────

class UKNationalGasExtractor(BaseExtractor):
    """
    National Gas NTS actual demand via the public operationaldata API.

    Endpoint: POST https://api.nationalgas.com/operationaldata/v1/publications/gasday
    Publication: PUBOBJ1030 — "Demand Actual, NTS, D+1 (Energy)"
    Values in kWh; converted to TWh (÷ 1e9). No authentication required.

    Requests are batched in 365-day chunks to stay within API limits.
    """
    country  = "GB"
    source   = "National Gas (NTS actual demand)"
    API_URL  = "https://api.nationalgas.com/operationaldata/v1/publications/gasday"
    PUB_ID   = "PUBOBJ1030"
    CHUNK    = 365

    def _fetch_chunk(self, from_date: date, to_date: date) -> list[dict]:
        try:
            r = requests.post(
                self.API_URL,
                json={
                    "publicationIds": [self.PUB_ID],
                    "fromDate": from_date.isoformat(),
                    "toDate":   to_date.isoformat(),
                },
                headers={**HEADERS, "Content-Type": "application/json"},
                timeout=30,
            )
            r.raise_for_status()
            pubs = r.json()
            records = []
            for pub in pubs:
                for entry in pub.get("publications", []):
                    d_str = entry.get("applicableFor", "")[:10]
                    val   = entry.get("value")
                    if not d_str or val is None:
                        continue
                    try:
                        records.append({
                            "date": date.fromisoformat(d_str),
                            "twh":  float(val) / 1e9,
                        })
                    except (ValueError, TypeError):
                        continue
            return records
        except Exception as e:
            logger.warning(f"National Gas API {from_date}–{to_date}: {e}")
            return []

    def _fetch(self, from_date: date, to_date: date) -> pd.DataFrame:
        all_records = []
        chunk_start = from_date
        while chunk_start <= to_date:
            chunk_end = min(
                date(chunk_start.year + (1 if chunk_start.month == 12 else 0),
                     (chunk_start.month % 12) + 1, 1) - timedelta(days=1),
                to_date,
            )
            # simpler: just step by CHUNK days
            chunk_end = min(chunk_start + timedelta(days=self.CHUNK - 1), to_date)
            all_records.extend(self._fetch_chunk(chunk_start, chunk_end))
            chunk_start = chunk_end + timedelta(days=1)

        if not all_records:
            return pd.DataFrame()
        df = pd.DataFrame(all_records)
        df = df[(df["date"] >= from_date) & (df["date"] <= to_date)]
        return df.groupby("date", as_index=False)["twh"].sum().sort_values("date").reset_index(drop=True)


# ── Italy — Snam Rete Gas ─────────────────────────────────────────────────────

class ItalySnamExtractor(BaseExtractor):
    """
    Italy — gas consumption via ENTSOG Transparency Platform.

    The Snam download (consumo_giornaliero.xls) is no longer available publicly;
    Snam moved operational data to their authenticated Jarvis platform.

    Uses three ENTSOG aggregated exit points (Allocation, daily, kWh/day):
      DIS-00005 — SRG delivery to distribution networks
      FNC-00005 — Industrial consumers (direct transmission offtake)
      FNC-00006 — Thermal plants (power generation)
    Sum validated against Eurostat IC_OBS at ~4-8% below (remaining gap is
    transmission own-use / network losses not reported on ENTSOG TP).
    """
    country = "IT"
    source  = "ENTSOG / Snam Rete Gas (DIS+FNC)"

    ENTSOG_BASE = "https://transparency.entsog.eu/api/v1"
    POINT_KEYS  = ["DIS-00005", "FNC-00005", "FNC-00006"]

    def _fetch_point(self, point_key: str, from_date: date, to_date: date) -> pd.DataFrame:
        params = {
            "pointKey":   point_key,
            "indicator":  "Allocation",
            "periodType": "day",
            "timezone":   "CET",
            "from":       from_date.isoformat(),
            "to":         to_date.isoformat(),
            "limit":      10000,
            "format":     "json",
        }
        for attempt in range(3):
            try:
                r = requests.get(
                    f"{self.ENTSOG_BASE}/operationalData",
                    params=params, headers=HEADERS,
                    timeout=90 + attempt * 30,   # 90s, 120s, 150s
                )
                r.raise_for_status()
                rows = r.json().get("operationalData", [])
                records = []
                for row in rows:
                    val = row.get("value")
                    period = row.get("periodFrom", "")[:10]
                    if val is None or val == "" or not period:
                        continue
                    records.append({"date": period, "twh": float(val) / 1e9})
                df = pd.DataFrame(records)
                if not df.empty:
                    df["date"] = pd.to_datetime(df["date"]).dt.date
                return df
            except requests.exceptions.Timeout:
                if attempt < 2:
                    wait = (attempt + 1) * 15
                    logger.warning(
                        f"ENTSOG Italy ({point_key}): timeout, "
                        f"retry {attempt + 1}/3 in {wait}s"
                    )
                    time.sleep(wait)
                else:
                    logger.warning(
                        f"ENTSOG Italy ({point_key}): timeout after 3 attempts, skipping"
                    )
            except Exception as e:
                logger.warning(f"ENTSOG Italy ({point_key}): {e}")
                break
        return pd.DataFrame()

    def _fetch(self, from_date: date, to_date: date) -> pd.DataFrame:
        dfs = [self._fetch_point(k, from_date, to_date) for k in self.POINT_KEYS]
        combined = pd.concat([d for d in dfs if not d.empty], ignore_index=True)
        if combined.empty:
            return pd.DataFrame()
        combined = combined[(combined["date"] >= from_date) & (combined["date"] <= to_date)]
        return combined.groupby("date", as_index=False)["twh"].sum()


# ── Spain — Enagas ────────────────────────────────────────────────────────────

class SpainEnagasExtractor(BaseExtractor):
    """
    Enagas Spain daily national gas demand via their transparency portal.

    The JSON API returns ~80 days of data per call (starting ~2.5 months before
    the requested date). For longer ranges we make overlapping calls.

    Endpoint: /realdemand_copy_copy.realdemand.json?date=DD/MM/YYYY
    Response: {"actual": [{"fecha_demanda": "YYYY-MM-DD", "demanda": "<GWh>"}, ...]}
    """
    country = "ES"
    source  = "Enagas"

    API_URL = (
        "https://www.enagas.es/content/enagas/es/gestion-tecnica-sistema"
        "/energy-data/demanda/historico/jcr:content/responsiveGrid"
        "/container_copy_19796/realdemand_copy_copy.realdemand.json"
    )
    WINDOW_DAYS  = 60   # 60-day step + up to 14-day retry = 74 days covered per window,
                        # ensuring no gap even in the worst case (next window ≥ 60 days back).
    HISTORY_FROM = date(2023, 1, 1)  # API returns no data for dates before 2023

    def _fetch(self, from_date: date, to_date: date) -> pd.DataFrame:
        from_date = max(from_date, self.HISTORY_FROM)
        # Use a dict keyed by date string to prevent double-counting when
        # overlapping windows return the same date (last write wins, which is fine
        # since Enagas returns the same value for a given date regardless of anchor).
        records: dict[str, float] = {}
        anchor = to_date
        seen_anchors: set = set()
        while anchor >= from_date:
            if anchor in seen_anchors:
                break
            seen_anchors.add(anchor)

            # Enagas sometimes returns 500/empty for very recent dates.
            # Retry with progressively earlier anchors (up to 14 days back).
            effective_anchor = None
            for offset in range(15):
                attempt = anchor - timedelta(days=offset)
                if attempt < from_date:
                    break
                params = {"date": attempt.strftime("%d/%m/%Y")}
                try:
                    r = requests.get(
                        self.API_URL, params=params, headers=HEADERS, timeout=60
                    )
                    r.raise_for_status()
                    data = r.json()
                    rows = data.get("actual", [])
                    if not rows:
                        continue  # empty response — try earlier date
                    for row in rows:
                        fecha = row.get("fecha_demanda", "")[:10]
                        demanda = row.get("demanda")
                        if not fecha or demanda is None:
                            continue
                        try:
                            records[fecha] = float(demanda) / 1000
                        except (TypeError, ValueError):
                            continue
                    effective_anchor = attempt
                    break
                except requests.exceptions.Timeout:
                    logger.debug(f"Enagas {attempt}: timeout, trying earlier anchor")
                    time.sleep(2)
                    continue
                except requests.exceptions.HTTPError as e:
                    if r.status_code in (500, 502, 503, 504):
                        logger.debug(f"Enagas {attempt}: {r.status_code}, trying earlier")
                        continue
                    logger.warning(f"Enagas {attempt}: {e}")
                    break
                except Exception as e:
                    logger.warning(f"Enagas {attempt}: {e}")
                    break

            if effective_anchor is None:
                logger.warning(f"Enagas: no data for window ending {anchor}")

            anchor = anchor - timedelta(days=self.WINDOW_DAYS)

        if not records:
            return pd.DataFrame()

        df = pd.DataFrame(
            [{"date": d, "twh": v} for d, v in records.items()]
        )
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df = df[(df["date"] >= from_date) & (df["date"] <= to_date)]
        return df.sort_values("date").reset_index(drop=True)


# ── Czech Republic — OTE ──────────────────────────────────────────────────────

class CzechOTEExtractor(BaseExtractor):
    """
    Stub — not used. OTE's old ZIP URL (/cs/statistika/plynovy-trh) was removed
    when the site restructured in late 2024. The replacement aggregated imbalance
    Excel reports "Off-take from System Total Volume" which includes transit
    flows through CZ to neighbouring countries — NOT domestic consumption only.
    Czech consumption is sourced from ENTSOG Physical Flow (DIS-00208 + FNC-00215)
    registered directly in pipeline.py instead.
    """
    country = "CZ"
    source  = "OTE (Czech gas market operator)"

    def _fetch(self, from_date: date, to_date: date) -> pd.DataFrame:
        logger.debug("CzechOTEExtractor: stub — CZ data comes from ENTSOG Physical Flow")
        return pd.DataFrame()


# ── Denmark — Energi Data Service ────────────────────────────────────────────

class DenmarkEnergiDataExtractor(BaseExtractor):
    """
    Energi Data Service (run by Energinet) — Gasflow dataset.
    Each record is one gas day with breakdown of sources/sinks in kWh.

    Domestic consumption = KWhToDenmark (negative convention: gas leaving
    the transmission system into distribution networks is recorded as negative).
    We take its absolute value.

    Known limitation (~20-25% structural undercount): biogas injected directly
    into Evida distribution networks bypasses Energinet transmission metering
    and is not included here. The GasSystemRightNow dataset would close this
    gap but its API returns 403 for automated access.
    """
    country = "DK"
    source  = "Energi Data Service (Energinet)"

    API_URL = "https://api.energidataservice.dk/dataset/Gasflow"

    def _fetch(self, from_date: date, to_date: date) -> pd.DataFrame:
        params = {
            "start": from_date.isoformat(),
            "end":   to_date.isoformat(),
            "limit": 10000,
            "sort":  "GasDay asc",
        }
        try:
            r = requests.get(self.API_URL, params=params, headers=HEADERS, timeout=30)
            r.raise_for_status()
            data = r.json()
            records_raw = data.get("records", [])
            if not records_raw:
                return pd.DataFrame()

            df = pd.DataFrame(records_raw)
            df["date"] = pd.to_datetime(df["GasDay"]).dt.date
            # KWhToDenmark is negative (gas leaving transmission → distribution/consumption)
            df["twh"] = (
                pd.to_numeric(df["KWhToDenmark"], errors="coerce").abs() / 1e9
            )
            df = df[(df["date"] >= from_date) & (df["date"] <= to_date)]
            df = df[df["twh"] > 0]
            return df.groupby("date", as_index=False)["twh"].sum()
        except Exception as e:
            logger.warning(f"Energi Data Service: {e}")
            return pd.DataFrame()


# ── Sweden — Energi Data Service (Energinet) ─────────────────────────────────

class SwedenEnergidataExtractor(BaseExtractor):
    """
    Sweden shares a balancing zone with Denmark; all Swedish gas supply crosses
    the DK→SE interconnection at Dragør. The Energinet Gasflow dataset publishes
    daily `KWhToSweden` (negative convention) which equals Swedish gas demand
    plus any net storage injection at the Skallen UGS facility.

    No Swedish domestic production exists, so net border flow ≈ consumption.
    The storage component is small (~1–3% of consumption). History from 2018.
    """
    country     = "SE"
    source_type = "flow_derived"
    source  = "Energi Data Service (Energinet) — KWhToSweden"
    method  = "flow_derived"

    API_URL = "https://api.energidataservice.dk/dataset/Gasflow"

    def _fetch(self, from_date: date, to_date: date) -> pd.DataFrame:
        params = {
            "start": from_date.isoformat(),
            "end":   to_date.isoformat(),
            "limit": 10000,
            "sort":  "GasDay asc",
        }
        try:
            r = requests.get(self.API_URL, params=params, headers=HEADERS, timeout=30)
            r.raise_for_status()
            records_raw = r.json().get("records", [])
            if not records_raw:
                return pd.DataFrame()
            df = pd.DataFrame(records_raw)
            df["date"] = pd.to_datetime(df["GasDay"]).dt.date
            df["twh"] = pd.to_numeric(df["KWhToSweden"], errors="coerce").abs() / 1e9
            df = df[(df["date"] >= from_date) & (df["date"] <= to_date)]
            df = df[df["twh"] > 0]
            return df.groupby("date", as_index=False)["twh"].sum()
        except Exception as e:
            logger.warning(f"Energi Data Service (SE): {e}")
            return pd.DataFrame()


# ── Austria — AGGM ───────────────────────────────────────────────────────────

class AustriaAGGMExtractor(BaseExtractor):
    """
    AGGM vis-service JSON API — ErmittelterEKVOesterreich time series.
    Total determined consumption Austria, all consumer categories, in kWh/day.
    Gas day boundary: 06:00 CET. History from 2019. Lag ~1 day. No auth required.
    """
    country = "AT"
    source  = "AGGM (Austrian Gas Grid Management)"

    API_URL    = "https://platform.aggm.at/vis-service/api/ts/values"
    TIMESERIES = "ErmittelterEKVOesterreich"

    def _fetch(self, from_date: date, to_date: date) -> pd.DataFrame:
        payload = {
            "rangeType":  "individual",
            "from":       f"{from_date.isoformat()}T06:00:00",
            "to":         f"{(to_date + timedelta(days=1)).isoformat()}T06:00:00",
            "granularity": "Day",
            "timeseries": [self.TIMESERIES],
        }
        try:
            r = requests.post(
                self.API_URL, json=payload, headers=HEADERS, timeout=60
            )
            r.raise_for_status()
            chart = (
                r.json()
                .get("timeSeriesData", {})
                .get("chartData", [{}])[0]
                .get("dataSet", [])
            )
            records = []
            for point in chart:
                x = point.get("x")
                y = point.get("y")
                if x is None or y is None:
                    continue
                d = pd.Timestamp(x, unit="ms", tz="UTC").tz_convert("Europe/Vienna").date()
                records.append({"date": d, "twh": float(y) / 1e9})
            if not records:
                return pd.DataFrame()
            df = pd.DataFrame(records)
            df = df[(df["date"] >= from_date) & (df["date"] <= to_date)]
            return df.sort_values("date").reset_index(drop=True)
        except Exception as e:
            logger.warning(f"AGGM: {e}")
            return pd.DataFrame()


# ── Estonia — Elering ────────────────────────────────────────────────────────

class EstoniaEleringExtractor(BaseExtractor):
    """
    Elering Dashboard API — /api/gas-system
    Returns hourly total domestic gas flow from the Estonian transmission
    network in kWh. Maximum request window is 1 year; we chunk accordingly.
    Validated against Eurostat IC_OBS: ~2-3% delta for comparable months.
    """
    country = "EE"
    source  = "Elering (Estonian TSO)"

    API_URL = "https://dashboard.elering.ee/api/gas-system"

    def _fetch(self, from_date: date, to_date: date) -> pd.DataFrame:
        from datetime import datetime as dt

        records = []
        start = from_date
        while start <= to_date:
            # Max 1 year per request
            end = min(to_date, date(start.year + 1, start.month, start.day) - timedelta(days=1))
            params = {
                "start": start.strftime("%Y-%m-%dT00:00:00Z"),
                "end":   (end + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z"),
            }
            try:
                r = requests.get(self.API_URL, params=params, headers=HEADERS, timeout=60)
                r.raise_for_status()
                for item in r.json().get("data", []):
                    ts  = item.get("timestamp")
                    val = item.get("value")
                    if ts is not None and val is not None:
                        records.append({
                            "date": dt.utcfromtimestamp(ts).date(),
                            "kwh":  float(val),
                        })
            except Exception as e:
                logger.warning(f"Elering gas-system {start}: {e}")
            start = end + timedelta(days=1)

        if not records:
            return pd.DataFrame()

        df = pd.DataFrame(records)
        df = df[(df["date"] >= from_date) & (df["date"] <= to_date)]
        df = df.groupby("date", as_index=False)["kwh"].sum()
        df["twh"] = df["kwh"] / 1e9
        return df[["date", "twh"]]


# ── Lithuania — Amber Grid ────────────────────────────────────────────────────

class LithuaniaAmberGridExtractor(BaseExtractor):
    """
    Amber Grid (Lithuanian TSO) open data portal — domestic consumption.

    Endpoint: /en/lietuvos-suvartojimo-duomenu-skaiciuokle/755/search
    Returns JSON with daily kWh totals for:
      - Transmitted to distribution systems
      - Transmitted to directly connected consumers
    We use the combined ltsuvartojimas_val field.

    Data coverage starts ~Oct 2021. Validated against Eurostat IC_OBS at
    -2% to -16% (own-use / losses gap, consistent directional offset).
    """
    country = "LT"
    source  = "Amber Grid (Lithuanian TSO)"

    API_URL = (
        "https://ambergrid.lt/en/"
        "lietuvos-suvartojimo-duomenu-skaiciuokle/755/search"
    )
    _HEADERS = {
        **HEADERS,
        "Referer": "https://ambergrid.lt/en/for-clients/open-data/650",
    }

    def _fetch(self, from_date: date, to_date: date) -> pd.DataFrame:
        # Chunk into monthly requests to stay within API limits
        from calendar import monthrange
        records = []
        cur = from_date.replace(day=1)
        while cur <= to_date:
            last_day = monthrange(cur.year, cur.month)[1]
            chunk_end = min(to_date, cur.replace(day=last_day))
            params = {
                "date_from": cur.isoformat(),
                "date_to":   chunk_end.isoformat(),
                "format":    "csv",
                "services":  "ltsuvartojimas",
            }
            try:
                r = requests.get(
                    self.API_URL, params=params,
                    headers=self._HEADERS, timeout=30,
                )
                r.raise_for_status()
                for row in r.json().get("list", []):
                    dt  = row.get("prsk_data_f")
                    val = row.get("ltsuvartojimas_val")
                    if dt and val is not None:
                        records.append({
                            "date": dt,
                            "twh":  float(val) / 1e9,
                        })
            except Exception as e:
                logger.warning(f"Amber Grid {cur.isoformat()}: {e}")
            # Advance to next month
            if cur.month == 12:
                cur = cur.replace(year=cur.year + 1, month=1, day=1)
            else:
                cur = cur.replace(month=cur.month + 1, day=1)

        if not records:
            return pd.DataFrame()

        df = pd.DataFrame(records)
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df = df[(df["date"] >= from_date) & (df["date"] <= to_date)]
        return df.groupby("date", as_index=False)["twh"].sum()


# ── Netherlands — GTS via ENTSOG ──────────────────────────────────────────────

class NetherlandsGTSExtractor(BaseExtractor):
    """
    Netherlands — gas consumption via ENTSOG Transparency Platform.

    GTS (Gasunie Transport Services) no longer publishes its own transparency
    portal; all data is on ENTSOG TP (confirmed at gts.nl/transparency).

    The generic ENTSOGDirectExtractor fails for NL because GTS does not publish
    Nomination data. The correct indicator is 'Physical Flow', filtered to
    the two domestic exit categories:
      - Distribution        → gas to local distribution companies (residential,
                              small commercial, small industrial)
      - Final Consumers     → large industrial users and power plants connected
                              directly to the GTS transmission system

    Excludes: Storage (injection/withdrawal), LNG terminals, cross-border exports.

    Requests are chunked year-by-year to avoid hitting ENTSOG result limits.
    Own-use gap vs Eurostat IC_OBS is expected at ~4–8% (compression fuel,
    line-pack, unaccounted-for gas not reported via Physical Flow).
    """
    country     = "NL"
    source      = "ENTSOG / GTS (Gasunie Transport Services)"
    source_type = "transmission_allocation"

    ENTSOG_BASE      = "https://transparency.entsog.eu/api/v1"
    OPERATOR_KEY     = "NL-TSO-0001"
    DOMESTIC_CATS    = {"Distribution", "Final Consumers"}

    def _fetch(self, from_date: date, to_date: date) -> pd.DataFrame:
        records = []
        year = from_date.year
        while year <= to_date.year:
            y_start = date(year, 1, 1) if year > from_date.year else from_date
            y_end   = date(year, 12, 31) if year < to_date.year else to_date
            params = {
                "operatorKey":  self.OPERATOR_KEY,
                "directionKey": "exit",
                "indicator":    "Physical Flow",
                "periodType":   "day",
                "from":         y_start.isoformat(),
                "to":           y_end.isoformat(),
                "format":       "json",
                "limit":        5000,
            }
            try:
                r = requests.get(
                    f"{self.ENTSOG_BASE}/aggregatedData",
                    params=params, headers=HEADERS, timeout=90,
                )
                r.raise_for_status()
                for row in r.json().get("aggregatedData", []):
                    if row.get("adjacentSystemsKey") not in self.DOMESTIC_CATS:
                        continue
                    val    = row.get("value")
                    period = (row.get("periodFrom") or "")[:10]
                    if val is None or not period:
                        continue
                    records.append({"date": period, "twh": float(val) / 1e9})
            except Exception as e:
                logger.warning(f"ENTSOG NL {year}: {e}")
            year += 1
            time.sleep(0.5)

        if not records:
            return pd.DataFrame()

        df = pd.DataFrame(records)
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df = df[(df["date"] >= from_date) & (df["date"] <= to_date)]
        return df.groupby("date", as_index=False)["twh"].sum()


# ── Portugal — REN Data Hub ────────────────────────────────────────────────────

class PortugalRENExtractor(BaseExtractor):
    """
    Portugal — total national gas consumption from REN Data Hub.

    Endpoint: servicebus.ren.pt/datahubapi/gas/GasConsumptionSupplyDaily
    Returns GWh/day. TOTAL_CONSUMPTION covers all end-user categories
    (conventional market + power generation + autonomous gas units).
    """

    country  = "PT"
    source   = "REN Data Hub (servicebus.ren.pt)"
    BASE_URL = "https://servicebus.ren.pt/datahubapi/gas/GasConsumptionSupplyDaily"

    def _fetch_day(self, d: date):
        try:
            r = requests.get(
                self.BASE_URL,
                params={"culture": "en-US", "date": d.isoformat()},
                headers=HEADERS,
                timeout=30,
            )
            r.raise_for_status()
            for item in r.json():
                if item.get("type") == "TOTAL_CONSUMPTION":
                    val = item.get("daily_Accumulation")
                    if val is not None:
                        return {"date": d, "twh": float(val) / 1000}
        except Exception as e:
            logger.warning(f"REN PT {d}: {e}")
        return None

    def _fetch(self, from_date: date, to_date: date) -> pd.DataFrame:
        all_dates = []
        d = from_date
        while d <= to_date:
            all_dates.append(d)
            d += timedelta(days=1)

        records = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(self._fetch_day, day) for day in all_dates]
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                if result is not None:
                    records.append(result)

        if not records:
            return pd.DataFrame()
        df = pd.DataFrame(records)
        df = df.sort_values("date").reset_index(drop=True)
        return df


# ── Croatia — Plinacro SUKAP ─────────────────────────────────────────────────

class CroatiaPlinacroExtractor(BaseExtractor):
    """
    Croatia — total daily consumption from Plinacro SUKAP portal.

    Endpoint: POST https://www.sukap.plinacro.hr/pub/consumption/search
    Body: {"gasDay": "YYYY-MM-DDT00:00:00"}

    Returns 24 hourly records. The last record's cumulativeCapacity is the
    daily total in kWh (gas day 07:15 – 06:15 CET). Fetched in parallel.
    """
    country  = "HR"
    source   = "Plinacro SUKAP"
    API_URL  = "https://www.sukap.plinacro.hr/pub/consumption/search"

    def _fetch_day(self, d: date):
        try:
            r = requests.post(
                self.API_URL,
                json={"gasDay": f"{d.isoformat()}T00:00:00"},
                headers={**HEADERS, "Content-Type": "application/json"},
                timeout=30,
            )
            r.raise_for_status()
            values = r.json().get("data", {}).get("values", [])
            if not values:
                return None
            last = max(values, key=lambda v: v.get("gasHour", -1))
            cumulative = last.get("cumulativeCapacity")
            if cumulative is None or cumulative <= 0:
                return None
            return {"date": d, "twh": float(cumulative) / 1e9}
        except Exception as e:
            logger.warning(f"Plinacro HR {d}: {e}")
            return None

    def _fetch(self, from_date: date, to_date: date) -> pd.DataFrame:
        all_dates = []
        d = from_date
        while d <= to_date:
            all_dates.append(d)
            d += timedelta(days=1)

        records = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(self._fetch_day, day) for day in all_dates]
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                if result is not None:
                    records.append(result)

        if not records:
            return pd.DataFrame()
        df = pd.DataFrame(records)
        return df.sort_values("date").reset_index(drop=True)


# ── Germany — Trading Hub Europe (public website API) ─────────────────────────

class GermanyTHEWebExtractor(BaseExtractor):
    """
    Germany — daily total gas consumption from Trading Hub Europe's public
    website chart API. No authentication required.

    Endpoint: api.tradinghub.eu/api/website/evoq/GetAggregierteVerbrauchsdatenChart
    Parameter: DatumStart=MM/YYYY — returns all days in that month.

    Covers the full German market area (H-Gas + L-Gas):
      SLP (Standardlastprofil) — residential, commercial, small industrial
      RLM (Registrierende Leistungsmessung) — large industrial, power plants

    Data quality hierarchy per day:
      Clearing (final, post-clearing period) → Corrected (M+12WD) → Allocation (D+1)
    Recent months (~last 3 months) are at Allocation level (preliminary).

    Units: kWh/day → TWh (÷ 1e9). History from January 2018.
    """
    country  = "DE"
    source   = "Trading Hub Europe (THE) — aggregated consumption data"
    method   = "direct"

    API_URL  = ("https://api.tradinghub.eu/api/website/evoq/"
                "GetAggregierteVerbrauchsdatenChart")
    _HEADERS = {
        **HEADERS,
        "Referer": ("https://www.tradinghub.eu/en-gb/Publications/"
                    "Further-Publications/Aggregated-Consumption-Data"),
        "Accept":  "application/json",
    }

    def _fetch_month(self, year: int, month: int) -> list[dict]:
        param = f"{month:02d}/{year}"
        try:
            r = requests.get(
                self.API_URL,
                params={"DatumStart": param},
                headers=self._HEADERS,
                timeout=30,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.warning(f"THE DE {param}: {e}")
            return []

    @staticmethod
    def _day_twh(row: dict) -> float:
        slp_h = (row.get("slP_Clearing_H") or row.get("slP_Corrected_H")
                 or row.get("slP_Allocation_H") or 0)
        slp_l = (row.get("slP_Clearing_L") or row.get("slP_Corrected_L")
                 or row.get("slP_Allocation_L") or 0)
        rlm_h = (row.get("rlM_Clearing_H") or row.get("rlM_Corrected_H")
                 or row.get("rlM_Allocation_H") or 0)
        rlm_l = (row.get("rlM_Clearing_L") or row.get("rlM_Corrected_L")
                 or row.get("rlM_Allocation_L") or 0)
        # Newer months use granular sub-fields when rollup fields are null
        if slp_h == 0 and rlm_h == 0:
            slp_h = (row.get("slPsyn_H_Gas") or 0) + (row.get("slPana_H_Gas") or 0)
            slp_l = (row.get("slPsyn_L_Gas") or 0) + (row.get("slPana_L_Gas") or 0)
            rlm_h = (row.get("rlMmT_H_Gas") or 0) + (row.get("rlMoT_H_Gas") or 0)
            rlm_l = (row.get("rlMmT_L_Gas") or 0) + (row.get("rlMoT_L_Gas") or 0)
        return (slp_h + slp_l + rlm_h + rlm_l) / 1e9

    def _fetch(self, from_date: date, to_date: date) -> pd.DataFrame:
        records = []
        # Iterate month by month
        year, month = from_date.year, from_date.month
        while (year, month) <= (to_date.year, to_date.month):
            rows = self._fetch_month(year, month)
            for row in rows:
                gastag = row.get("gastag", "")[:10]
                if not gastag:
                    continue
                try:
                    d = date.fromisoformat(gastag)
                except ValueError:
                    continue
                if d < from_date or d > to_date:
                    continue
                twh = self._day_twh(row)
                if twh > 0:
                    records.append({"date": d, "twh": twh})
            # Advance to next month
            if month == 12:
                year, month = year + 1, 1
            else:
                month += 1
            time.sleep(0.2)

        if not records:
            return pd.DataFrame()
        df = pd.DataFrame(records)
        return df.sort_values("date").reset_index(drop=True)


# ── Greece — DESFA validated daily off-takes ──────────────────────────────────

class GreeceDesfaExtractor(BaseExtractor):
    """
    Greece — validated daily gas off-takes from DESFA (Hellenic Gas Transmission
    System Operator) public Excel file.

    DESFA publishes Flows.xlsx on their transparency page — a rolling file that
    covers 2008 to the most recent validated gas day. It contains all exit point
    off-takes (kWh at 25°C combustion reference temperature) across both entry
    points (Agia Triada, Kipi, Nea Mesimvria, Sidirokastro, Amfitriti) and
    ~53 domestic off-take exit points (city gates, industrial consumers, CCGTs).

    This replaces the ENTSOG Physical Flow approach (51 exit points) which was
    missing unpublished domestic city-gate exits and undercounted by ~-37%.
    DESFA's own metered totals are the authoritative source.

    File: https://www.desfa.gr/wp-content/uploads/2024/08/Flows.xlsx
    Sheet: kWh_25oC — rows 0–4 are header, row -1 is TOTAL; data rows 5 to -2.
    Units: kWh (25°C GCV) → TWh (÷ 1e9).
    """
    country   = "GR"
    source    = "DESFA (Hellenic Gas Transmission System Operator)"
    method    = "direct"

    FILE_URL  = "https://www.desfa.gr/wp-content/uploads/2024/08/Flows.xlsx"
    SHEET     = "kWh_25oC"
    _HEADERS  = {
        **HEADERS,
        "Referer": "https://www.desfa.gr/",
    }

    def _fetch(self, from_date: date, to_date: date) -> pd.DataFrame:
        try:
            r = requests.get(self.FILE_URL, headers=self._HEADERS, timeout=90)
            r.raise_for_status()
        except Exception as e:
            logger.warning(f"DESFA Flows.xlsx download: {e}")
            return pd.DataFrame()

        try:
            xl   = pd.ExcelFile(BytesIO(r.content))
            raw  = xl.parse(self.SHEET, header=None)

            # Rows 0–4 = headers; last row = TOTAL aggregate — skip both ends
            data = raw.iloc[5:-1].copy()
            data.columns = range(len(data.columns))

            # Column 0 = date, column 6 = NaN separator between entry/exit groups
            # Exit (off-take) columns: 7 onward
            data = data[pd.to_datetime(data[0], errors="coerce").notna()].copy()
            data["date"] = pd.to_datetime(data[0]).dt.date

            exit_cols = list(range(7, len(data.columns) - 1))  # -1: excludes 'date'
            data["twh"] = (
                data[exit_cols]
                .apply(pd.to_numeric, errors="coerce")
                .sum(axis=1) / 1e9
            )

            df = data[["date", "twh"]].copy()
            df = df[(df["date"] >= from_date) & (df["date"] <= to_date)]
            df = df[df["twh"] > 0]
            return df.sort_values("date").reset_index(drop=True)
        except Exception as e:
            logger.warning(f"DESFA Flows.xlsx parse: {e}")
            return pd.DataFrame()


# ── Finland — Gasgrid transparency Excel ─────────────────────────────────────

class FinlandGasgridExtractor(BaseExtractor):
    """
    Finland — daily gas consumption from Gasgrid's published Excel file.

    Gasgrid publishes a monthly-updated Excel on their transparency page:
    https://gasgrid.fi/en/gas-business/transparency-and-market-information/

    The file covers completed months only (typically updated ~1 week after
    month end). For the current incomplete month, the pipeline falls back to
    FinlandLNGExtractor (flow-derived) via the method-rank deduplication in
    pipeline.py.

    Units in the Excel: GWh/day (GCV basis). We convert to TWh (÷ 1000).
    """
    country    = "FI"
    source     = "Gasgrid Finland"
    method     = "direct"
    INDEX_URL  = "https://gasgrid.fi/en/gas-business/transparency-and-market-information/"

    def _find_excel_url(self) -> str | None:
        try:
            r = requests.get(self.INDEX_URL, headers=HEADERS, timeout=30)
            r.raise_for_status()
            import re
            matches = re.findall(
                r'https://gasgrid\.fi/wp-content/uploads/[^"\']+Gas-consumption-in-Finland[^"\']+\.xlsx',
                r.text,
            )
            return matches[0] if matches else None
        except Exception as e:
            logger.warning(f"Gasgrid index fetch: {e}")
            return None

    def _parse_excel(self, content: bytes, from_date: date, to_date: date) -> pd.DataFrame:
        xls = pd.ExcelFile(BytesIO(content))
        parts = []
        for sheet in xls.sheet_names:
            try:
                df = xls.parse(sheet, header=None)
                # col 2 = date, col 3 = GWh/day
                sub = df[[2, 3]].dropna(subset=[2, 3]).copy()
                sub.columns = ["date", "twh"]
                sub["date"] = pd.to_datetime(sub["date"], errors="coerce").dt.date
                sub["twh"]  = pd.to_numeric(sub["twh"], errors="coerce") / 1000  # GWh → TWh
                sub = sub.dropna()
                parts.append(sub)
            except Exception:
                continue
        if not parts:
            return pd.DataFrame()
        combined = pd.concat(parts, ignore_index=True)
        combined = combined[(combined["date"] >= from_date) & (combined["date"] <= to_date)]
        return combined.sort_values("date").reset_index(drop=True)

    def _fetch(self, from_date: date, to_date: date) -> pd.DataFrame:
        url = self._find_excel_url()
        if not url:
            logger.warning("Gasgrid: could not locate Excel URL on transparency page")
            return pd.DataFrame()
        try:
            r = requests.get(url, headers=HEADERS, timeout=60)
            r.raise_for_status()
            return self._parse_excel(r.content, from_date, to_date)
        except Exception as e:
            logger.warning(f"Gasgrid Excel download: {e}")
            return pd.DataFrame()
