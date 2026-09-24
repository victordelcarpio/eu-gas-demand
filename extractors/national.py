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
    National Gas (formerly National Grid Gas Transmission) daily demand data.

    As of 2025, the MIP portal (mip-prd-web.azurewebsites.net) has been replaced
    by a React SPA at data.nationalgas.com. The new download API
    (/api/find-gas-data-download) requires OAuth credentials obtained from:
        https://apideveloper.nationalgas.com/s/

    Set env var NATIONAL_GAS_API_TOKEN to a valid Bearer token to enable.
    Without it this extractor returns empty.
    """
    country = "GB"
    source  = "National Gas / Xoserve"

    DOWNLOAD_URL = "https://data.nationalgas.com/api/find-gas-data-download"

    def _fetch(self, from_date: date, to_date: date) -> pd.DataFrame:
        import os
        token = os.environ.get("NATIONAL_GAS_API_TOKEN", "").strip()
        if not token:
            logger.warning(
                "UKNationalGasExtractor: NATIONAL_GAS_API_TOKEN not set. "
                "The National Gas data portal now requires OAuth credentials. "
                "Register at https://apideveloper.nationalgas.com/s/ to obtain "
                "an API token, then set the NATIONAL_GAS_API_TOKEN env var."
            )
            return pd.DataFrame()

        params = {
            "dateFrom":    from_date.isoformat(),
            "dateTo":      to_date.isoformat(),
            "dateType":    "GASDAY",
            "applicableFor": "Y",
            "latestFlag":  "N",
            "ids":         "PUBOBJ1030",  # Demand Actual, NTS, D+1 (Energy)
            "type":        "csv",
        }
        auth_headers = {**HEADERS, "Authorization": f"Bearer {token}"}
        try:
            r = requests.get(
                self.DOWNLOAD_URL, params=params, headers=auth_headers, timeout=30
            )
            r.raise_for_status()

            lines = r.text.strip().splitlines()
            header_idx = 0
            for i, line in enumerate(lines):
                low = line.lower()
                if "," in line and any(k in low for k in ("date", "value", "demand")):
                    header_idx = i
                    break
            df = pd.read_csv(StringIO("\n".join(lines[header_idx:])))
            df.columns = [c.strip().lower() for c in df.columns]

            date_col  = next(c for c in df.columns if "date" in c)
            value_col = next(
                c for c in df.columns if "value" in c or "demand" in c or "quantity" in c
            )
            df["date"] = pd.to_datetime(df[date_col], dayfirst=True, errors="coerce").dt.date
            df["twh"]  = pd.to_numeric(df[value_col], errors="coerce") / 1000
            df = df.dropna(subset=["date", "twh"])
            df = df[(df["date"] >= from_date) & (df["date"] <= to_date)]
            return df.groupby("date", as_index=False)["twh"].sum()
        except Exception as e:
            logger.warning(f"National Gas API: {e}")
            return pd.DataFrame()


# ── Italy — Snam Rete Gas ─────────────────────────────────────────────────────

class ItalySnamExtractor(BaseExtractor):
    """
    Italy — gas consumption via ENTSOG Transparency Platform.

    The Snam download (consumo_giornaliero.xls) is no longer available publicly;
    Snam moved operational data to their authenticated Jarvis platform.

    Fallback: ENTSOG distribution exit point DIS-00005 ("SRG DELIVERY TO
    DISTRIBUTION NETWORKS", operator IT-TSO-0001 / Snam Rete Gas).
    Indicator: Allocation (daily, kWh/day). Verified working as of Sep 2026.
    """
    country = "IT"
    source  = "ENTSOG / Snam Rete Gas (DIS-00005)"

    ENTSOG_BASE = "https://transparency.entsog.eu/api/v1"
    POINT_KEY   = "DIS-00005"

    def _fetch(self, from_date: date, to_date: date) -> pd.DataFrame:
        params = {
            "pointKey":  self.POINT_KEY,
            "indicator": "Allocation",
            "periodType": "day",
            "timezone":  "CET",
            "from":      from_date.isoformat(),
            "to":        to_date.isoformat(),
            "limit":     10000,
            "format":    "json",
        }
        try:
            r = requests.get(
                f"{self.ENTSOG_BASE}/operationalData",
                params=params, headers=HEADERS, timeout=60,
            )
            r.raise_for_status()
            data = r.json()
            rows = data.get("operationalData", [])
            if not rows:
                return pd.DataFrame()

            records = []
            for row in rows:
                val = row.get("value")
                if val is None or val == "":
                    continue
                period = row.get("periodFrom", "")[:10]
                if not period:
                    continue
                records.append({
                    "date": period,
                    "twh":  float(val) / 1e9,  # kWh/day → TWh
                })

            df = pd.DataFrame(records)
            if df.empty:
                return df
            df["date"] = pd.to_datetime(df["date"]).dt.date
            df = df[(df["date"] >= from_date) & (df["date"] <= to_date)]
            return df.groupby("date", as_index=False)["twh"].sum()
        except Exception as e:
            logger.warning(f"ENTSOG Italy (DIS-00005): {e}")
            return pd.DataFrame()


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
    WINDOW_DAYS = 80

    def _fetch(self, from_date: date, to_date: date) -> pd.DataFrame:
        records = []
        anchor = to_date
        seen_anchors: set = set()
        while anchor >= from_date:
            if anchor in seen_anchors:
                break
            seen_anchors.add(anchor)

            # Enagas sometimes returns 500/empty for very recent dates.
            # Retry with progressively earlier anchors (up to 14 days back)
            # so we don't skip an entire 80-day window.
            effective_anchor = None
            for offset in range(15):
                attempt = anchor - timedelta(days=offset)
                if attempt < from_date:
                    break
                params = {"date": attempt.strftime("%d/%m/%Y")}
                try:
                    r = requests.get(
                        self.API_URL, params=params, headers=HEADERS, timeout=30
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
                            records.append({"date": fecha, "twh": float(demanda) / 1000})
                        except (TypeError, ValueError):
                            continue
                    effective_anchor = attempt
                    break
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

        df = pd.DataFrame(records)
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df = df[(df["date"] >= from_date) & (df["date"] <= to_date)]
        return df.groupby("date", as_index=False)["twh"].sum()


# ── Czech Republic — OTE ──────────────────────────────────────────────────────

class CzechOTEExtractor(BaseExtractor):
    """
    OTE (Czech gas and electricity market operator) publishes daily
    gas evaluations as ZIP files containing CSV data.

    V0 evaluation: published D+3, provisional
    V1 evaluation: published 16th of following month, more final

    Days are fetched concurrently (up to 16 threads) to keep runtime
    manageable over multi-year ranges. Each thread downloads one day's ZIP.
    """
    country = "CZ"
    source  = "OTE (Czech gas market operator)"

    BASE_URL = "https://www.ote-cr.cz/cs/statistika/plynovy-trh"
    MAX_WORKERS = 16

    def _fetch(self, from_date: date, to_date: date) -> pd.DataFrame:
        import concurrent.futures as cf

        days = [
            from_date + timedelta(days=i)
            for i in range((to_date - from_date).days + 1)
        ]

        records = []
        with cf.ThreadPoolExecutor(max_workers=self.MAX_WORKERS) as pool:
            results = {pool.submit(self._fetch_day, d): d for d in days}
            for future, d in results.items():
                try:
                    twh = future.result(timeout=30)
                    if twh is not None:
                        records.append({"date": d, "twh": twh})
                except Exception as e:
                    logger.debug(f"OTE {d}: {e}")

        return pd.DataFrame(records) if records else pd.DataFrame()

    def _fetch_day(self, day: date) -> float | None:
        """Try V1 first (more accurate), fall back to V0."""
        for version in ["V1", "V0"]:
            url = (
                f"{self.BASE_URL}/{version}/"
                f"{version}_{day.strftime('%Y%m%d')}.zip"
            )
            try:
                r = requests.get(url, headers=HEADERS, timeout=20)
                if r.status_code == 404:
                    continue
                r.raise_for_status()
                with zipfile.ZipFile(BytesIO(r.content)) as z:
                    csv_name = next(
                        (n for n in z.namelist() if n.endswith(".csv")), None
                    )
                    if not csv_name:
                        continue
                    with z.open(csv_name) as f:
                        df = pd.read_csv(f, sep=";", decimal=",",
                                         encoding="utf-8-sig", header=0)
                        val_col = next(
                            (c for c in df.columns
                             if "spotřeba" in c.lower() or "consumption" in c.lower()
                             or "celkem" in c.lower() or "spot" in c.lower()),
                            None
                        )
                        if val_col is None:
                            continue
                        total_mwh = pd.to_numeric(df[val_col], errors="coerce").sum()
                        return float(total_mwh) / 1_000_000  # MWh → TWh
            except Exception as e:
                logger.debug(f"OTE {version} {day}: {e}")
        return None


# ── Denmark — Energi Data Service ────────────────────────────────────────────

class DenmarkEnergiDataExtractor(BaseExtractor):
    """
    Energi Data Service (run by Energinet) — Gasflow dataset.
    Each record is one gas day with breakdown of sources/sinks in kWh.

    Domestic consumption = KWhToDenmark (negative convention: gas leaving
    the transmission system into distribution networks is recorded as negative).
    We take its absolute value.
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


# ── Austria — AGGM ───────────────────────────────────────────────────────────

class AustriaAGGMExtractor(BaseExtractor):
    """
    AGGM (Austrian Gas Grid Management) publishes daily consumption data.
    Bruegel uses the WIFO-aggregated CSV; we go direct to AGGM.
    """
    country = "AT"
    source  = "AGGM (Austrian Gas Grid Management)"

    # AGGM publishes consumption via their transparency data portal
    API_URL = "https://www.aggm.at/aggm/gst/transparency/gasday"

    def _fetch(self, from_date: date, to_date: date) -> pd.DataFrame:
        params = {
            "dateFrom": from_date.strftime("%Y-%m-%d"),
            "dateTo":   to_date.strftime("%Y-%m-%d"),
            "type":     "consumption",
            "format":   "json",
        }
        try:
            r = requests.get(self.API_URL, params=params, headers=HEADERS, timeout=30)
            r.raise_for_status()
            data = r.json()
            records = []
            for row in data:
                records.append({
                    "date": pd.to_datetime(row.get("gasDay", "")).date(),
                    "twh":  float(row.get("value", 0)) / 1_000_000,  # MWh → TWh
                })
            df = pd.DataFrame(records)
            if df.empty:
                return df
            return df[(df["date"] >= from_date) & (df["date"] <= to_date)]
        except Exception as e:
            logger.warning(f"AGGM: {e}")
            return pd.DataFrame()
