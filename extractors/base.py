"""
Base extractor class.

Every extractor returns a DataFrame with exactly these columns:
    date        : datetime.date       — day of consumption
    country     : str                 — ISO2 (DE, FR, GB, IT, ES, NL, CZ, SK, ...)
    twh         : float               — consumption in TWh
    source      : str                 — human-readable source label
    provisional : bool                — True if data may be revised
    method      : str                 — "direct" | "flow_derived"
"""

from abc import ABC, abstractmethod
from datetime import date, timedelta
import pandas as pd
import logging

logger = logging.getLogger(__name__)

SCHEMA = ["date", "country", "twh", "source", "provisional", "method"]

# Provisional flag: any data within this many days of today is marked provisional
PROVISIONAL_LAG_DAYS = {
    "DE": 2,
    "FR": 5,
    "GB": 2,
    "IT": 3,
    "ES": 3,
    "NL": 3,
    "CZ": 4,
    "DK": 2,
    "AT": 3,
    # Flow-derived countries — ENTSOG lag
    "SK": 3, "LV": 3, "LT": 3, "EE": 3, "FI": 3, "SE": 3,
}


class BaseExtractor(ABC):
    country: str       # ISO2
    source:  str       # display label
    method:  str = "direct"

    def fetch(self, from_date: date, to_date: date) -> pd.DataFrame:
        """
        Public entry point. Calls _fetch(), validates schema, adds provisional flag.
        Returns empty DataFrame (with correct columns) on failure.
        """
        try:
            df = self._fetch(from_date, to_date)
            df = self._validate(df)
            df = self._mark_provisional(df)
            logger.info(f"[{self.country}] {self.source}: {len(df)} rows "
                        f"({df['date'].min()} → {df['date'].max()})")
            return df
        except Exception as e:
            logger.warning(f"[{self.country}] {self.source} failed: {e}")
            return self._empty()

    @abstractmethod
    def _fetch(self, from_date: date, to_date: date) -> pd.DataFrame:
        """Implement per-source. Return DataFrame with SCHEMA columns (provisional optional)."""
        ...

    def _validate(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return self._empty()
        df["date"]        = pd.to_datetime(df["date"]).dt.date
        df["country"]     = self.country
        df["source"]      = self.source
        df["method"]      = self.method
        df["twh"]         = pd.to_numeric(df["twh"], errors="coerce")
        df["provisional"] = df.get("provisional", False)
        df = df.dropna(subset=["date", "twh"])
        df = df[df["twh"] >= 0]
        return df[SCHEMA]

    def _mark_provisional(self, df: pd.DataFrame) -> pd.DataFrame:
        lag = PROVISIONAL_LAG_DAYS.get(self.country, 5)
        cutoff = date.today() - timedelta(days=lag)
        df["provisional"] = df["date"] > cutoff
        return df

    def _empty(self) -> pd.DataFrame:
        return pd.DataFrame(columns=SCHEMA)
