"""
Flow-derived consumption for zero-domestic-production countries:
SK, LV, LT, EE, SE — via ENTSOG border flow mass balance
FI               — via ALSI LNG sendout (post-2022 primary supply)

Method:
    consumption ≈ Σ(entry flows) − Σ(exit flows) ± storage change

For countries with no domestic production and no net storage change
(SE, EE) the mass balance simplifies to:
    consumption ≈ Σ(entry flows) − Σ(exit cross-border exits)

Storage adjustment (where material):
    SK — POZAGAS + NAFTA (both facilities) via AGSI country aggregate (no auth needed)
    LV — now uses FNC-00205 direct point, so no storage correction needed here

ALSI (LNG):
    FI — Inkoo + Hamina terminals
    LT — Klaipeda FSRU (cross-check)
"""

from datetime import date
import os
import pandas as pd
import requests
import logging
from .base import BaseExtractor
from .entsog import fetch_border_flows

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger(__name__)

AGSI_BASE = "https://agsi.gie.eu/api"
ALSI_BASE = "https://alsi.gie.eu/api"

# Countries for which we apply a AGSI country-level storage correction.
# AGSI country endpoint is public (no API key needed) and aggregates all
# facilities in the country.  Units returned: GWh/day for injection/withdrawal.
AGSI_STORAGE_COUNTRIES = {"SK"}  # POZAGAS + NAFTA combined

# ALSI terminal EIC codes for Finland
FINLAND_LNG_EICS = [
    "21W000000000369S",  # Inkoo (Gasgrid / Excelerate)
    "21W000000000370V",  # Hamina (Gasum)
]


def _fetch_agsi_country_storage(country_code: str, from_date: date, to_date: date) -> pd.DataFrame:
    """
    Fetch daily aggregate storage injection/withdrawal for a country from AGSI.
    Requires GIE_AGSI_API_KEY environment variable (free registration at agsi.gie.eu/account).

    Returns DataFrame with columns [date, net_storage] where net_storage is
    in TWh/day; positive = net withdrawal (adds to consumption).

    AGSI reports injection/withdrawal in GWh/day → divide by 1000 for TWh.
    """
    api_key = os.environ.get("GIE_AGSI_API_KEY", "").strip()
    if not api_key:
        logger.warning(
            f"AGSI storage correction skipped for {country_code}: "
            "GIE_AGSI_API_KEY not set. Register at agsi.gie.eu/account for a free key."
        )
        return pd.DataFrame()

    params = {
        "country": country_code,
        "from":    from_date.isoformat(),
        "to":      to_date.isoformat(),
        "size":    730,
        "format":  "json",
    }
    headers = {"x-key": api_key}
    try:
        r = requests.get(AGSI_BASE, params=params, headers=headers, timeout=30)
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            logger.warning(f"AGSI API error for {country_code}: {data.get('message', data.get('error'))}")
            return pd.DataFrame()
    except Exception as e:
        logger.warning(f"AGSI country storage fetch failed for {country_code}: {e}")
        return pd.DataFrame()

    records = []
    for row in data.get("data", []):
        gas_day    = row.get("gasDayStart", "")[:10]
        injection  = float(row.get("injection",  0) or 0)
        withdrawal = float(row.get("withdrawal", 0) or 0)
        records.append({
            "date":        gas_day,
            "net_storage": (withdrawal - injection) / 1000,  # GWh → TWh
        })

    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


_ALSI_RETRY_DELAYS = [15, 30]


def _fetch_alsi_sendout(eic: str, from_date: date, to_date: date) -> pd.DataFrame:
    """Fetch LNG sendout (regasification) from ALSI in TWh/day."""
    params = {
        "type":   "lng",
        "eic":    eic,
        "from":   from_date.isoformat(),
        "to":     to_date.isoformat(),
        "size":   500,
        "format": "json",
    }
    import time
    for attempt, delay in enumerate([0] + _ALSI_RETRY_DELAYS):
        if delay:
            logger.info(f"ALSI retry for {eic} in {delay}s (attempt {attempt+1})")
            time.sleep(delay)
        try:
            r = requests.get(ALSI_BASE, params=params, timeout=60)
            r.raise_for_status()
            data = r.json()
            break
        except Exception as e:
            if attempt < len(_ALSI_RETRY_DELAYS):
                logger.debug(f"ALSI sendout {eic} attempt {attempt+1}: {e}")
                continue
            logger.warning(f"ALSI sendout fetch failed for {eic}: {e}")
            return pd.DataFrame()
    else:
        return pd.DataFrame()

    rows = data.get("data", [])
    records = []
    for row in rows:
        gas_day = row.get("gasDayStart", "")[:10]
        sendout = float(row.get("send_out", 0) or 0)
        records.append({"date": gas_day, "twh": sendout})

    df = pd.DataFrame(records)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


class SlovakiaExtractor(BaseExtractor):
    """
    Slovakia consumption = ENTSOG aggregated border balance + AGSI storage correction.

    Uses the ENTSOG /aggregatedData endpoint (country=SK, indicator=Allocation) which
    correctly nets bidirectional transit flows — giving net Slovak domestic supply from
    cross-border Transmission interconnections. Point-level ITP data cannot be used
    because transit points report identical entry/exit values, causing net=0 for transit.

    Storage correction via AGSI (GIE_AGSI_API_KEY env var) adds/subtracts daily net
    storage drawdown (POZAGAS + NAFTA combined). Without the key, summer values will
    be understated because the border balance alone undercounts when injection > entry.
    """
    country     = "SK"
    source      = "ENTSOG aggregated balance + AGSI storage (SK)"
    method      = "flow_derived"
    source_type = "flow_derived"

    ENTSOG_AGG = "https://transparency.entsog.eu/api/v1/aggregatedData"
    _HEADERS   = {"User-Agent": "Mozilla/5.0 (compatible; gas-demand-pipeline/1.0; research use)"}

    def _fetch_entsog_balance(self, from_date: date, to_date: date) -> pd.DataFrame:
        params = {
            "countryKey":  "SK",
            "indicator":   "Allocation",
            "periodType":  "day",
            "from":        from_date.isoformat(),
            "to":          to_date.isoformat(),
            "limit":       2000,
            "format":      "json",
        }
        try:
            r = requests.get(self.ENTSOG_AGG, params=params, headers=self._HEADERS, timeout=90)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            logger.warning(f"SK ENTSOG aggregated fetch failed: {e}")
            return pd.DataFrame()

        daily: dict[str, dict] = {}
        for row in data.get("aggregatedData", []):
            d = row.get("periodFrom", "")[:10]
            direction = row.get("directionKey", "")
            val = float(row.get("value") or 0)
            if d not in daily:
                daily[d] = {"entry": 0.0, "exit": 0.0}
            daily[d][direction] += val

        if not daily:
            return pd.DataFrame()

        records = [{"date": d, "net_twh": (v["entry"] - v["exit"]) / 1e9} for d, v in daily.items()]
        df = pd.DataFrame(records)
        df["date"] = pd.to_datetime(df["date"]).dt.date
        return df.sort_values("date").reset_index(drop=True)

    def _fetch(self, from_date: date, to_date: date) -> pd.DataFrame:
        balance = self._fetch_entsog_balance(from_date, to_date)

        dates = pd.date_range(from_date, to_date, freq="D")
        df = pd.DataFrame({"date": [d.date() for d in dates]})

        if not balance.empty:
            df = df.merge(balance, on="date", how="left")
        else:
            df["net_twh"] = 0.0
        df["net_twh"] = df["net_twh"].fillna(0)

        # Storage correction from AGSI (requires GIE_AGSI_API_KEY)
        storage_net = pd.Series(0.0, index=df.index)
        s = _fetch_agsi_country_storage("SK", from_date, to_date)
        if not s.empty:
            s_merged = df[["date"]].merge(s, on="date", how="left")
            storage_net = s_merged["net_storage"].fillna(0)
            logger.info(
                f"SK: AGSI storage correction applied "
                f"({s['net_storage'].sum():.3f} TWh net over period)"
            )

        df["twh"]        = (df["net_twh"] + storage_net.values).clip(lower=0)
        df["provisional"] = False
        return df[["date", "twh", "provisional"]]


class FlowDerivedExtractor(BaseExtractor):
    """
    Consumption = border entries − border exits ± storage change.
    Used for: SE (border-only), FI (pre-LNG).
    For SK use SlovakiaExtractor instead.
    """
    method      = "flow_derived"
    source_type = "flow_derived"

    def __init__(self, country: str):
        self.country = country
        self.source  = f"ENTSOG flows ({country})"

    def _fetch(self, from_date: date, to_date: date) -> pd.DataFrame:
        flows = fetch_border_flows(self.country, from_date, to_date)
        entries = flows.get("entry", pd.DataFrame())
        exits   = flows.get("exit",  pd.DataFrame())

        dates = pd.date_range(from_date, to_date, freq="D")
        df = pd.DataFrame({"date": dates})
        df["date"] = df["date"].dt.date

        if not entries.empty:
            df = df.merge(entries.rename(columns={"twh": "entry_twh"}), on="date", how="left")
        else:
            df["entry_twh"] = 0.0

        if not exits.empty:
            df = df.merge(exits.rename(columns={"twh": "exit_twh"}), on="date", how="left")
        else:
            df["exit_twh"] = 0.0

        df["entry_twh"] = df["entry_twh"].fillna(0)
        df["exit_twh"]  = df["exit_twh"].fillna(0)
        df["twh"]        = (df["entry_twh"] - df["exit_twh"]).clip(lower=0)
        df["provisional"] = False

        return df[["date", "twh", "provisional"]]


class FinlandLNGExtractor(BaseExtractor):
    """
    Finland-specific: post-2022 supply is almost entirely LNG via
    Inkoo and Hamina terminals. Use ALSI sendout as consumption proxy.
    Also adds any residual pipeline flows from ENTSOG (small).
    """
    country     = "FI"
    source      = "ALSI LNG sendout + ENTSOG (FI)"
    method      = "flow_derived"
    source_type = "flow_derived"

    def _fetch(self, from_date: date, to_date: date) -> pd.DataFrame:
        dates = pd.date_range(from_date, to_date, freq="D")
        df = pd.DataFrame({"date": [d.date() for d in dates]})
        df["twh"] = 0.0

        # LNG sendout from both terminals
        for eic in FINLAND_LNG_EICS:
            lng = _fetch_alsi_sendout(eic, from_date, to_date)
            if not lng.empty:
                merged = df[["date"]].merge(lng, on="date", how="left")
                df["twh"] += merged["twh"].fillna(0).values

        # Add any residual pipeline flows (Balticconnector from EE, small post-2022)
        flows = fetch_border_flows("FI", from_date, to_date)
        entries = flows.get("entry", pd.DataFrame())
        if not entries.empty:
            merged = df[["date"]].merge(
                entries.rename(columns={"twh": "pipeline_twh"}), on="date", how="left"
            )
            df["twh"] += merged["pipeline_twh"].fillna(0).values

        df["twh"]         = df["twh"].clip(lower=0)
        df["provisional"] = False
        return df[["date", "twh", "provisional"]]
