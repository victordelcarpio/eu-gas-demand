"""
ENTSOG Transparency Platform extractor.

Used for:
  - Direct consumption: NL, BE, PL, and any country reporting
    consumption at distribution exit points
  - Flow derivation input: SK, LV, LT, EE, FI, SE
    (handled separately in flow_derived.py, which calls this)

API docs: https://transparency.entsog.eu/api/v1/
"""

from datetime import date
import pandas as pd
import requests
import logging
from .base import BaseExtractor

logger = logging.getLogger(__name__)

ENTSOG_BASE = "https://transparency.entsog.eu/api/v1"

# Indicator labels used on the platform
CONSUMPTION_INDICATOR = "Physical Consumption"
FLOW_INDICATOR        = "Physical Flow"

# Country codes as ENTSOG uses them (mostly ISO2, GB not UK)
ENTSOG_COUNTRY_MAP = {
    "NL": "NL", "BE": "BE", "PL": "PL",
    "SK": "SK", "LV": "LV", "LT": "LT",
    "EE": "EE", "FI": "FI", "SE": "SE",
    "GB": "GB", "DE": "DE", "FR": "FR",
    "IT": "IT", "ES": "ES", "AT": "AT",
    "CZ": "CZ", "HU": "HU", "RO": "RO",
}


def _query(endpoint: str, params: dict, timeout: int = 60) -> dict:
    """Raw ENTSOG API call with error handling."""
    url = f"{ENTSOG_BASE}/{endpoint}"
    try:
        r = requests.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.Timeout:
        raise RuntimeError(f"ENTSOG timeout on {endpoint}")
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(f"ENTSOG HTTP {e.response.status_code} on {endpoint}")
    except Exception as e:
        raise RuntimeError(f"ENTSOG error: {e}")


def fetch_aggregated_consumption(
    country: str,
    from_date: date,
    to_date: date,
    period_type: str = "day",
) -> pd.DataFrame:
    """
    Fetch aggregated consumption data for a country from ENTSOG.
    Returns daily TWh by country.
    """
    params = {
        "indicator":  CONSUMPTION_INDICATOR,
        "periodType": period_type,
        "timezone":   "CET",
        "from":       from_date.isoformat(),
        "to":         to_date.isoformat(),
        "limit":      10000,
        "format":     "json",
    }
    # Filter to country if the API supports it
    entsog_code = ENTSOG_COUNTRY_MAP.get(country)
    if entsog_code:
        params["countryKey"] = entsog_code

    data = _query("aggregatedData", params)
    rows = data.get("AggregatedData", [])
    if not rows:
        return pd.DataFrame()

    records = []
    for row in rows:
        # Filter to our target country
        if row.get("tsoCountry") != entsog_code:
            continue
        val_kwh_per_day = row.get("value")
        if val_kwh_per_day is None:
            continue
        period_from = row.get("periodFrom", "")[:10]
        period_to   = row.get("periodTo",   "")[:10]
        if not period_from:
            continue

        # Convert kWh/day → TWh for the period
        if period_type == "day":
            twh = float(val_kwh_per_day) / 1e9
        else:
            # Monthly: value is total kWh for the month
            twh = float(val_kwh_per_day) / 1e9

        records.append({
            "date":    period_from,
            "country": country,
            "twh":     twh,
        })

    df = pd.DataFrame(records)
    if df.empty:
        return df

    # Aggregate across operators/points for the same date
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df = df.groupby(["date", "country"], as_index=False)["twh"].sum()
    return df


def fetch_border_flows(
    country: str,
    from_date: date,
    to_date: date,
) -> dict[str, pd.DataFrame]:
    """
    Fetch entry and exit physical flows at border points for a country.
    Returns {"entry": df, "exit": df} with daily TWh.
    Used by flow_derived.py for mass-balance countries.
    """
    result = {}
    for direction in ["entry", "exit"]:
        params = {
            "indicator":     FLOW_INDICATOR,
            "periodType":    "day",
            "timezone":      "CET",
            "from":          from_date.isoformat(),
            "to":            to_date.isoformat(),
            "limit":         10000,
            "format":        "json",
            "pointDirection": direction,
        }
        entsog_code = ENTSOG_COUNTRY_MAP.get(country)
        if entsog_code:
            params["countryKey"] = entsog_code

        try:
            data  = _query("operationalData", params)
            rows  = data.get("Operational", [])
        except Exception as e:
            logger.warning(f"ENTSOG {direction} flows for {country}: {e}")
            result[direction] = pd.DataFrame()
            continue

        records = []
        for row in rows:
            val = row.get("value")
            if val is None:
                continue
            period = row.get("periodFrom", "")[:10]
            if not period:
                continue
            # Only cross-border interconnection points (not distribution exits)
            point_type = row.get("infrastructureTypeLabel", "")
            if "Interconnection" not in point_type and "LNG" not in point_type:
                continue
            records.append({
                "date": period,
                "twh":  float(val) / 1e9,
            })

        df = pd.DataFrame(records)
        if not df.empty:
            df["date"] = pd.to_datetime(df["date"]).dt.date
            df = df.groupby("date", as_index=False)["twh"].sum()
        result[direction] = df

    return result


class ENTSOGDirectExtractor(BaseExtractor):
    """
    For countries where ENTSOG aggregated consumption is reliable:
    NL, BE, PL, and others reporting via distribution exit points.
    """
    method = "direct"

    def __init__(self, country: str):
        self.country = country
        self.source  = f"ENTSOG ({country})"

    def _fetch(self, from_date: date, to_date: date) -> pd.DataFrame:
        df = fetch_aggregated_consumption(self.country, from_date, to_date)
        if df.empty:
            return df
        df["provisional"] = False  # set by base class
        return df
