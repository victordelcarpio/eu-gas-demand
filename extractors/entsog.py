"""
ENTSOG Transparency Platform extractor.

Used for:
  - Border flow derivation: SK, LV, LT, EE, FI, SE
    (handled separately in flow_derived.py, which calls this)

API notes (verified Sep 2026):
  - /operationalData requires a specific pointKey to return data; country-level
    queries always return empty. Two-step approach: first query /interconnections
    to discover border point keys, then fetch per-point flow data.
  - /AggregatedData endpoint does not return data; not used.
  - Response key for operationalData queries is "operationalData" (lowercase).
"""

from datetime import date
import pandas as pd
import requests
import logging
from .base import BaseExtractor

logger = logging.getLogger(__name__)

ENTSOG_BASE = "https://transparency.entsog.eu/api/v1"

FLOW_INDICATOR = "Physical Flow"

ENTSOG_COUNTRY_MAP = {
    "NL": "NL", "BE": "BE", "PL": "PL",
    "SK": "SK", "LV": "LV", "LT": "LT",
    "EE": "EE", "FI": "FI", "SE": "SE",
    "GB": "GB", "DE": "DE", "FR": "FR",
    "IT": "IT", "ES": "ES", "AT": "AT",
    "CZ": "CZ", "HU": "HU", "RO": "RO",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; gas-demand-pipeline/1.0; research use)"
}


def _query(endpoint: str, params: dict, timeout: int = 60) -> dict:
    """Raw ENTSOG API call with error handling."""
    url = f"{ENTSOG_BASE}/{endpoint}"
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.Timeout:
        raise RuntimeError(f"ENTSOG timeout on {endpoint}")
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(f"ENTSOG HTTP {e.response.status_code} on {endpoint}")
    except Exception as e:
        raise RuntimeError(f"ENTSOG error: {e}")


def _get_border_point_keys(country: str) -> dict[str, list[str]]:
    """
    Query /interconnections to find the border point keys for a country.
    Returns {"entry": [pointKeys...], "exit": [pointKeys...]}.
    Entry = points where gas flows INTO the country.
    Exit  = points where gas flows OUT OF the country.
    Only includes Transmission and LNG infrastructure types with data.
    """
    entsog_code = ENTSOG_COUNTRY_MAP.get(country)
    if not entsog_code:
        return {"entry": [], "exit": []}

    entry_keys = []
    exit_keys = []

    # Exit points: where country is the source (fromCountryKey)
    try:
        data = _query("interconnections", {
            "fromCountryKey": entsog_code,
            "limit": 200,
            "format": "json",
        })
        for ic in data.get("interconnections", []):
            infra = ic.get("fromInfrastructureTypeLabel", "")
            has_data = ic.get("fromHasData", False)
            pk = ic.get("fromPointKey")
            if pk and has_data and ("Transmission" in infra or "LNG" in infra or "Storage" in infra):
                exit_keys.append(pk)
    except Exception as e:
        logger.warning(f"ENTSOG interconnections (exit) for {country}: {e}")

    # Entry points: where country is the destination (toCountryKey)
    try:
        data = _query("interconnections", {
            "toCountryKey": entsog_code,
            "limit": 200,
            "format": "json",
        })
        for ic in data.get("interconnections", []):
            infra = ic.get("toInfrastructureTypeLabel", "")
            has_data = ic.get("toHasData", False)
            pk = ic.get("toPointKey")
            if pk and has_data and ("Transmission" in infra or "LNG" in infra or "Storage" in infra):
                entry_keys.append(pk)
    except Exception as e:
        logger.warning(f"ENTSOG interconnections (entry) for {country}: {e}")

    return {
        "entry": list(dict.fromkeys(entry_keys)),  # deduplicate, preserve order
        "exit":  list(dict.fromkeys(exit_keys)),
    }


def _fetch_point_flows(
    point_keys: list[str],
    direction: str,
    from_date: date,
    to_date: date,
) -> pd.DataFrame:
    """
    Fetch daily physical flow for a list of point keys, filtering to the given direction.
    Returns DataFrame with [date, twh] aggregated across all points.
    """
    all_records = []
    for pk in point_keys:
        params = {
            "pointKey":  pk,
            "indicator": FLOW_INDICATOR,
            "periodType": "day",
            "timezone":  "CET",
            "from":      from_date.isoformat(),
            "to":        to_date.isoformat(),
            "limit":     10000,
            "format":    "json",
        }
        try:
            data = _query("operationalData", params)
            rows = data.get("operationalData", [])
            for row in rows:
                if row.get("directionKey") != direction:
                    continue
                val = row.get("value")
                if val is None or val == "":
                    continue
                period = row.get("periodFrom", "")[:10]
                if not period:
                    continue
                try:
                    twh = float(val) / 1e9  # kWh/day → TWh
                except (TypeError, ValueError):
                    continue
                all_records.append({"date": period, "twh": twh})
        except Exception as e:
            logger.debug(f"ENTSOG {direction} flow for {pk}: {e}")

    if not all_records:
        return pd.DataFrame()

    df = pd.DataFrame(all_records)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df.groupby("date", as_index=False)["twh"].sum()


def fetch_border_flows(
    country: str,
    from_date: date,
    to_date: date,
) -> dict[str, pd.DataFrame]:
    """
    Fetch entry and exit physical flows at border points for a country.
    Returns {"entry": df, "exit": df} with daily TWh.
    Used by flow_derived.py for mass-balance countries.

    Two-step approach:
      1. Query /interconnections to discover border point keys.
      2. Query /operationalData for each point with Physical Flow indicator.
    """
    point_keys = _get_border_point_keys(country)

    result = {}
    for direction in ["entry", "exit"]:
        keys = point_keys[direction]
        if not keys:
            logger.warning(f"ENTSOG: no {direction} points found for {country}")
            result[direction] = pd.DataFrame()
            continue

        df = _fetch_point_flows(keys, direction, from_date, to_date)
        result[direction] = df

    return result


class ENTSOGDirectExtractor(BaseExtractor):
    """
    Placeholder extractor for countries that previously used the /AggregatedData
    endpoint (NL, BE, PL, HU, RO, GR, PT, HR, SI, BG).

    The /AggregatedData endpoint no longer returns data. This class is retained
    for registry compatibility but returns empty DataFrames until a replacement
    source is wired up per country.
    """
    method = "direct"

    def __init__(self, country: str):
        self.country = country
        self.source  = f"ENTSOG ({country})"

    def _fetch(self, from_date: date, to_date: date) -> pd.DataFrame:
        logger.warning(
            f"ENTSOG AggregatedData endpoint is not functional for {self.country}. "
            "No data returned. Wire up a national TSO source for this country."
        )
        return pd.DataFrame()
