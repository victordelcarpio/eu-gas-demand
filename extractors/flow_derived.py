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
    LV — Inčukalns UGS via AGSI+ (GIE API)
    LT — no significant storage
    SK — Láb UGS via AGSI+

ALSI (LNG):
    FI — Inkoo + Hamina terminals
    LT — Klaipeda FSRU (cross-check)
"""

from datetime import date
import pandas as pd
import requests
import logging
from .base import BaseExtractor
from .entsog import fetch_border_flows

logger = logging.getLogger(__name__)

AGSI_BASE = "https://agsi.gie.eu/api"
ALSI_BASE = "https://alsi.gie.eu/api"

# AGSI storage facility EIC codes for our countries
# Source: GIE AGSI+ facility list
STORAGE_EICS = {
    "LV": ["21W000000000010H"],  # Inčukalns (Latvia) — note: serves LV/EE/FI
    "SK": ["27W-SK-POZAGAS-A"],  # Láb (Slovakia)
}

# Countries where Inčukalns storage is partly allocated to (not just LV)
# We apportion by approximate share of withdrawals going to each country
INCUKALNS_SHARES = {"LV": 0.55, "EE": 0.25, "FI": 0.20}

# ALSI terminal EIC codes for Finland
FINLAND_LNG_EICS = [
    "21W000000000369S",  # Inkoo (Gasgrid / Excelerate)
    "21W000000000370V",  # Hamina (Gasum)
]


def _fetch_agsi_storage(eic: str, from_date: date, to_date: date) -> pd.DataFrame:
    """
    Fetch daily net storage flow (withdrawal - injection) in TWh from AGSI+.
    Positive = net withdrawal (adds to consumption), Negative = net injection.
    """
    params = {
        "type":     "storage",
        "eic":      eic,
        "from":     from_date.isoformat(),
        "to":       to_date.isoformat(),
        "size":     500,
        "format":   "json",
    }
    try:
        r = requests.get(AGSI_BASE, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning(f"AGSI storage fetch failed for {eic}: {e}")
        return pd.DataFrame()

    rows = data.get("data", [])
    records = []
    for row in rows:
        gas_day   = row.get("gasDayStart", "")[:10]
        injection = float(row.get("injection",   0) or 0)
        withdrawal= float(row.get("withdrawal",  0) or 0)
        records.append({
            "date":        gas_day,
            "net_storage": withdrawal - injection,  # positive = net withdrawal
        })

    df = pd.DataFrame(records)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


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
    try:
        r = requests.get(ALSI_BASE, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning(f"ALSI sendout fetch failed for {eic}: {e}")
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


class FlowDerivedExtractor(BaseExtractor):
    """
    Consumption = border entries − border exits ± storage change.
    For FI: LNG sendout is the primary inflow source.
    """
    method = "flow_derived"

    def __init__(self, country: str):
        self.country = country
        self.source  = f"ENTSOG flows ({country})"

    def _fetch(self, from_date: date, to_date: date) -> pd.DataFrame:
        flows = fetch_border_flows(self.country, from_date, to_date)
        entries = flows.get("entry", pd.DataFrame())
        exits   = flows.get("exit",  pd.DataFrame())

        # Build daily date spine
        dates = pd.date_range(from_date, to_date, freq="D")
        df = pd.DataFrame({"date": dates})
        df["date"] = df["date"].dt.date

        # Merge entry and exit flows
        if not entries.empty:
            df = df.merge(entries.rename(columns={"twh": "entry_twh"}),
                          on="date", how="left")
        else:
            df["entry_twh"] = 0.0

        if not exits.empty:
            df = df.merge(exits.rename(columns={"twh": "exit_twh"}),
                          on="date", how="left")
        else:
            df["exit_twh"] = 0.0

        df["entry_twh"] = df["entry_twh"].fillna(0)
        df["exit_twh"]  = df["exit_twh"].fillna(0)

        # Storage adjustment
        storage_eics = STORAGE_EICS.get(self.country, [])
        storage_net  = pd.Series(0.0, index=df.index)

        for eic in storage_eics:
            s = _fetch_agsi_storage(eic, from_date, to_date)
            if not s.empty:
                s_merged = df[["date"]].merge(s, on="date", how="left")
                share = INCUKALNS_SHARES.get(self.country, 1.0) if eic == "21W000000000010H" else 1.0
                storage_net += s_merged["net_storage"].fillna(0) * share

        df["storage_net"] = storage_net.values
        df["twh"]         = (df["entry_twh"] - df["exit_twh"] + df["storage_net"]).clip(lower=0)
        df["provisional"] = False  # set by base class

        return df[["date", "twh", "provisional"]]


class FinlandLNGExtractor(BaseExtractor):
    """
    Finland-specific: post-2022 supply is almost entirely LNG via
    Inkoo and Hamina terminals. Use ALSI sendout as consumption proxy.
    Also adds any residual pipeline flows from ENTSOG (small).
    """
    country = "FI"
    source  = "ALSI LNG sendout + ENTSOG (FI)"
    method  = "flow_derived"

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
