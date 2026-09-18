"""
National TSO / market operator extractors for direct consumption data.

Germany  : Trading Hub Europe (THE) — daily CSV
France   : GRTGaz — annual XLS files
UK       : National Gas (formerly NGT) — API
Italy    : Snam Rete Gas — API/CSV
Spain    : Enagas — API
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
    Trading Hub Europe publishes daily gas consumption (SLP + RLM) as CSV.
    URL pattern: https://www.tradinghub.eu/Portals/0/Verbrauch/Verbrauchsdaten_{YYYY}.csv
    Columns include: Datum, Verbrauch (MWh/h → convert to TWh/day)
    """
    country = "DE"
    source  = "Trading Hub Europe (THE)"

    def _fetch(self, from_date: date, to_date: date) -> pd.DataFrame:
        years = range(from_date.year, to_date.year + 1)
        dfs = []
        for year in years:
            url = f"https://www.tradinghub.eu/Portals/0/Verbrauch/Verbrauchsdaten_{year}.csv"
            try:
                r = requests.get(url, headers=HEADERS, timeout=30)
                r.raise_for_status()
                # THE CSV uses semicolon separator, German locale (comma decimals)
                raw = r.content.decode("utf-8-sig", errors="replace")
                df  = pd.read_csv(StringIO(raw), sep=";", decimal=",")
                dfs.append(df)
            except Exception as e:
                logger.warning(f"THE {year}: {e}")

        if not dfs:
            return pd.DataFrame()

        df = pd.concat(dfs, ignore_index=True)

        # Normalise column names (may vary by year)
        df.columns = [c.strip().lower() for c in df.columns]

        # Find date and value columns
        date_col  = next((c for c in df.columns if "datum" in c or "date" in c), None)
        value_col = next((c for c in df.columns if "verbrauch" in c or "consumption" in c), None)

        if not date_col or not value_col:
            raise ValueError(f"Unexpected THE columns: {df.columns.tolist()}")

        df = df[[date_col, value_col]].rename(columns={date_col: "date", value_col: "twh_raw"})
        df["date"] = pd.to_datetime(df["date"], dayfirst=True, errors="coerce").dt.date
        df["twh_raw"] = pd.to_numeric(df["twh_raw"], errors="coerce")

        # THE value is in MWh/h (hourly average) — multiply by 24 to get MWh/day, then /1e6 for TWh
        df["twh"] = df["twh_raw"] * 24 / 1_000_000

        df = df[(df["date"] >= from_date) & (df["date"] <= to_date)]
        df = df.groupby("date", as_index=False)["twh"].sum()
        return df


# ── France — GRTGaz ──────────────────────────────────────────────────────────

class FranceGRTGazExtractor(BaseExtractor):
    """
    GRTGaz publishes annual XLS files with daily consumption by sector.
    URL: https://www.grtgaz.com/fileadmin/.../{YYYY}-consommation-journaliere-grtgaz.xls
    We sum all sectors (residential, industry, power, other).
    """
    country = "FR"
    source  = "GRTGaz"

    # Note: URL pattern changes slightly by year — update as needed
    URL_TEMPLATE = (
        "https://www.grtgaz.com/fileadmin/plaquettes/fr/Statistiques/"
        "{year}/{year}-consommation-journaliere-grtgaz.xlsx"
    )

    def _fetch(self, from_date: date, to_date: date) -> pd.DataFrame:
        years = range(from_date.year, to_date.year + 1)
        dfs = []
        for year in years:
            url = self.URL_TEMPLATE.format(year=year)
            try:
                r = requests.get(url, headers=HEADERS, timeout=45)
                r.raise_for_status()
                xls = pd.ExcelFile(BytesIO(r.content))
                # GRTGaz file has one sheet per zone or a summary sheet
                # Try to find the main consumption sheet
                sheet = next(
                    (s for s in xls.sheet_names if "conso" in s.lower() or "total" in s.lower()),
                    xls.sheet_names[0]
                )
                df = xls.parse(sheet, header=0)
                dfs.append((year, df))
            except Exception as e:
                logger.warning(f"GRTGaz {year}: {e}")

        if not dfs:
            return pd.DataFrame()

        records = []
        for year, df in dfs:
            # Find date column and numeric columns
            df.columns = [str(c).strip() for c in df.columns]
            date_col = next((c for c in df.columns if "date" in c.lower() or "jour" in c.lower()), None)
            if not date_col:
                continue
            numeric_cols = [c for c in df.columns if c != date_col and
                            pd.to_numeric(df[c], errors="coerce").notna().sum() > 50]
            df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
            df = df.dropna(subset=[date_col])
            for _, row in df.iterrows():
                total_gwh = sum(
                    pd.to_numeric(row.get(c, 0), errors="coerce") or 0
                    for c in numeric_cols
                )
                records.append({
                    "date": row[date_col].date(),
                    "twh":  total_gwh / 1000  # GWh → TWh
                })

        result = pd.DataFrame(records)
        if result.empty:
            return result
        result = result[(result["date"] >= from_date) & (result["date"] <= to_date)]
        return result.groupby("date", as_index=False)["twh"].sum()


# ── UK — National Gas ─────────────────────────────────────────────────────────

class UKNationalGasExtractor(BaseExtractor):
    """
    National Gas (formerly National Grid Gas Transmission) publishes
    daily demand data via their data portal API.
    Endpoint: https://api.nationalgrid.com/gas/...
    Falls back to Xoserve/Elexon data where needed.
    """
    country = "GB"
    source  = "National Gas / Xoserve"

    DATA_URL = "https://mip-prd-web.azurewebsites.net/DataItemViewer/DownloadFile"

    def _fetch(self, from_date: date, to_date: date) -> pd.DataFrame:
        # National Gas Data Item: total system demand (D+1 publication)
        params = {
            "PublicationObjectKey": "4609325691964",
            "PublicationObjectName": "Total System Demand",
            "DateFrom": from_date.strftime("%d/%m/%Y"),
            "DateTo":   to_date.strftime("%d/%m/%Y"),
            "FileType": "csv",
        }
        try:
            r = requests.get(self.DATA_URL, params=params, headers=HEADERS, timeout=30)
            r.raise_for_status()
            df = pd.read_csv(StringIO(r.text))
            df.columns = [c.strip().lower() for c in df.columns]

            date_col  = next(c for c in df.columns if "date" in c)
            value_col = next(c for c in df.columns if "value" in c or "demand" in c)

            df["date"] = pd.to_datetime(df[date_col], dayfirst=True).dt.date
            # National Gas reports in GWh
            df["twh"]  = pd.to_numeric(df[value_col], errors="coerce") / 1000
            df = df[(df["date"] >= from_date) & (df["date"] <= to_date)]
            return df.groupby("date", as_index=False)["twh"].sum()
        except Exception as e:
            logger.warning(f"National Gas API: {e}")
            return pd.DataFrame()


# ── Italy — Snam Rete Gas ─────────────────────────────────────────────────────

class ItalySnamExtractor(BaseExtractor):
    """
    Snam publishes daily gas consumption data on their transparency portal.
    Data covers total national demand including power sector.
    """
    country = "IT"
    source  = "Snam Rete Gas"

    BASE_URL = "https://www.snam.it/it/trasporto/dati-operativi-business/dati-gas-naturale/"

    def _fetch(self, from_date: date, to_date: date) -> pd.DataFrame:
        # Snam API endpoint for operational data
        api_url = "https://www.snam.it/content/dam/snam-rebranding/Trasporto/it/dati-operativi-business/xls/consumo_giornaliero.xls"
        try:
            r = requests.get(api_url, headers=HEADERS, timeout=45)
            r.raise_for_status()
            xls = pd.read_excel(BytesIO(r.content), sheet_name=0, header=1)
            xls.columns = [str(c).strip() for c in xls.columns]

            date_col  = xls.columns[0]
            value_col = xls.columns[1]

            xls["date"] = pd.to_datetime(xls[date_col], errors="coerce").dt.date
            xls["twh"]  = pd.to_numeric(xls[value_col], errors="coerce")
            # Snam reports in Mm³/day — convert using ~10.55 kWh/m³
            xls["twh"]  = xls["twh"] * 1e6 * 10.55 / 1e12

            xls = xls[(xls["date"] >= from_date) & (xls["date"] <= to_date)]
            return xls.groupby("date", as_index=False)["twh"].sum()
        except Exception as e:
            logger.warning(f"Snam: {e}")
            return pd.DataFrame()


# ── Spain — Enagas ────────────────────────────────────────────────────────────

class SpainEnagasExtractor(BaseExtractor):
    """
    Enagas publishes daily gas demand via their transparency portal API.
    Reports total demand in GWh/day.
    """
    country = "ES"
    source  = "Enagas"

    def _fetch(self, from_date: date, to_date: date) -> pd.DataFrame:
        url = "https://www.enagas.es/enagas/es/Gestion_Tecnica_del_Sistema/Demanda_Gas_Natural"
        api = "https://www.enagas.es/enagas/secciones/gestion_tecnica_del_sistema/energia_en_el_sistema_gasista/demanda/"

        params = {
            "fechaInicio": from_date.strftime("%d/%m/%Y"),
            "fechaFin":    to_date.strftime("%d/%m/%Y"),
            "tipoDemanda": "total",
            "formato":     "csv",
        }
        try:
            r = requests.get(api, params=params, headers=HEADERS, timeout=30)
            r.raise_for_status()
            df = pd.read_csv(StringIO(r.text), sep=";", decimal=",")
            df.columns = [c.strip().lower() for c in df.columns]
            date_col  = next(c for c in df.columns if "fecha" in c or "date" in c)
            value_col = next(c for c in df.columns if "demanda" in c or "gwh" in c or "value" in c)
            df["date"] = pd.to_datetime(df[date_col], dayfirst=True).dt.date
            df["twh"]  = pd.to_numeric(df[value_col], errors="coerce") / 1000  # GWh → TWh
            df = df[(df["date"] >= from_date) & (df["date"] <= to_date)]
            return df.groupby("date", as_index=False)["twh"].sum()
        except Exception as e:
            logger.warning(f"Enagas: {e}")
            return pd.DataFrame()


# ── Czech Republic — OTE ──────────────────────────────────────────────────────

class CzechOTEExtractor(BaseExtractor):
    """
    OTE (Czech gas and electricity market operator) publishes daily
    gas evaluations as ZIP files containing CSV data.

    V0 evaluation: published D+3, provisional
    V1 evaluation: published 16th of following month, more final
    V2 evaluation: published 4 months later, final

    We download V0 for recent data (provisional=True) and V1/V2 where available.
    URL: https://www.ote-cr.cz/cs/statistika/plynovy-trh/V0/V0_{YYYYMMDD}.zip
    """
    country = "CZ"
    source  = "OTE (Czech gas market operator)"

    BASE_URL = "https://www.ote-cr.cz/cs/statistika/plynovy-trh"

    def _fetch(self, from_date: date, to_date: date) -> pd.DataFrame:
        records = []
        current = from_date
        while current <= to_date:
            twh = self._fetch_day(current)
            if twh is not None:
                records.append({"date": current, "twh": twh})
            current += timedelta(days=1)

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
                        # OTE CSV: rows are hours, column with total consumption
                        # Find consumption column (MWh)
                        val_col = next(
                            (c for c in df.columns
                             if "spot" in c.lower() or "spotřeba" in c.lower()
                             or "consumption" in c.lower() or "celkem" in c.lower()),
                            None
                        )
                        if val_col is None:
                            continue
                        total_mwh = pd.to_numeric(
                            df[val_col], errors="coerce"
                        ).sum()
                        return float(total_mwh) / 1_000_000  # MWh → TWh
            except Exception as e:
                logger.debug(f"OTE {version} {day}: {e}")
        return None


# ── Denmark — Energi Data Service ────────────────────────────────────────────

class DenmarkEnergiDataExtractor(BaseExtractor):
    """
    Energi Data Service (run by Energinet) provides daily gas flow data
    via a clean REST API with no authentication required.
    Dataset: Gasflow
    """
    country = "DK"
    source  = "Energi Data Service (Energinet)"

    API_URL = "https://api.energidataservice.dk/dataset/Gasflow"

    def _fetch(self, from_date: date, to_date: date) -> pd.DataFrame:
        params = {
            "start":  from_date.isoformat(),
            "end":    to_date.isoformat(),
            "limit":  10000,
            "sort":   "GasDay asc",
            "filter": '{"TypeOfFlow":["Consumption"]}',
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
            # Energinet reports in MWh/h — multiply by 24 for MWh/day → TWh
            df["twh"]  = pd.to_numeric(df.get("Quantity", df.columns[-1]),
                                        errors="coerce") * 24 / 1_000_000
            df = df[(df["date"] >= from_date) & (df["date"] <= to_date)]
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
