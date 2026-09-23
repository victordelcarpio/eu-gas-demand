"""
Pipeline orchestrator.

Usage:
    from pipeline import run
    df, coverage = run(from_date=date(2024, 9, 1), to_date=date.today())

Returns:
    df       : daily DataFrame with columns [date, country, twh, source, provisional, method]
    coverage : dict with per-country coverage summary for the footer/appendix
"""

import logging
import concurrent.futures
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

BASELINE_CACHE_PATH = Path("output/baseline_cache.csv")

from extractors.national import (
    GermanyTHEExtractor,
    FranceGRTGazExtractor,
    UKNationalGasExtractor,
    ItalySnamExtractor,
    SpainEnagasExtractor,
    CzechOTEExtractor,
    DenmarkEnergiDataExtractor,
    AustriaAGGMExtractor,
)
from extractors.entsog import ENTSOGDirectExtractor
from extractors.flow_derived import FlowDerivedExtractor, FinlandLNGExtractor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Extractor registry ────────────────────────────────────────────────────────
def _build_extractors():
    return [
        # Direct — national TSOs / market operators
        GermanyTHEExtractor(),
        FranceGRTGazExtractor(),
        UKNationalGasExtractor(),
        ItalySnamExtractor(),
        SpainEnagasExtractor(),
        CzechOTEExtractor(),
        DenmarkEnergiDataExtractor(),
        AustriaAGGMExtractor(),
        # Direct — ENTSOG aggregated consumption
        ENTSOGDirectExtractor("NL"),
        ENTSOGDirectExtractor("BE"),
        ENTSOGDirectExtractor("PL"),
        ENTSOGDirectExtractor("HU"),
        ENTSOGDirectExtractor("RO"),
        ENTSOGDirectExtractor("GR"),
        ENTSOGDirectExtractor("PT"),
        ENTSOGDirectExtractor("HR"),
        ENTSOGDirectExtractor("SI"),
        ENTSOGDirectExtractor("BG"),
        # Flow-derived — zero domestic production
        FlowDerivedExtractor("SK"),
        FlowDerivedExtractor("LV"),
        FlowDerivedExtractor("LT"),
        FlowDerivedExtractor("EE"),
        FlowDerivedExtractor("SE"),
        FinlandLNGExtractor(),
    ]


# ── Baseline computation ──────────────────────────────────────────────────────
def compute_baseline(df: pd.DataFrame) -> pd.DataFrame:
    """
    For each country, compute 2019-2021 average monthly demand.
    Returns a DataFrame with [country, month (1-12), twh_baseline].
    """
    hist = df[df["date"].apply(lambda d: 2019 <= d.year <= 2021)].copy()
    if hist.empty:
        return pd.DataFrame(columns=["country", "month", "twh_baseline"])

    hist["month"] = hist["date"].apply(lambda d: d.month)
    baseline = (
        hist.groupby(["country", "month", hist["date"].apply(lambda d: d.year)])["twh"]
        .sum()
        .reset_index()
        .groupby(["country", "month"])["twh"]
        .mean()
        .reset_index()
        .rename(columns={"twh": "twh_baseline"})
    )
    return baseline


# ── Coverage report ───────────────────────────────────────────────────────────
def build_coverage(df: pd.DataFrame, from_date: date, to_date: date) -> list[dict]:
    """
    Per-country coverage summary for appendix.
    """
    expected_days = (to_date - from_date).days + 1
    report = []
    for country, grp in df.groupby("country"):
        days_present  = grp["date"].nunique()
        last_date     = grp["date"].max()
        n_provisional = grp["provisional"].sum()
        sources       = grp["source"].unique().tolist()
        methods       = grp["method"].unique().tolist()
        missing_days  = expected_days - days_present
        report.append({
            "country":      country,
            "source":       " + ".join(sources),
            "method":       " + ".join(methods),
            "days_present": days_present,
            "days_missing": missing_days,
            "last_date":    last_date,
            "provisional":  int(n_provisional),
            "complete":     missing_days == 0,
        })
    return sorted(report, key=lambda x: x["country"])


# ── Baseline cache helpers ────────────────────────────────────────────────────
def _load_baseline_cache() -> Optional[pd.DataFrame]:
    if BASELINE_CACHE_PATH.exists():
        try:
            df = pd.read_csv(BASELINE_CACHE_PATH)
            df["date"] = pd.to_datetime(df["date"]).dt.date
            df["provisional"] = df["provisional"].astype(bool)
            logger.info(f"Baseline cache hit: {len(df)} rows from {BASELINE_CACHE_PATH}")
            return df
        except Exception as e:
            logger.warning(f"Could not load baseline cache: {e}")
    return None


def _save_baseline_cache(df: pd.DataFrame) -> None:
    try:
        BASELINE_CACHE_PATH.parent.mkdir(exist_ok=True)
        df.to_csv(BASELINE_CACHE_PATH, index=False)
        logger.info(f"Baseline cached to {BASELINE_CACHE_PATH} ({len(df)} rows)")
    except Exception as e:
        logger.warning(f"Could not save baseline cache: {e}")


# ── Main run function ─────────────────────────────────────────────────────────
def run(
    from_date: Optional[date] = None,
    to_date:   Optional[date] = None,
    baseline_from: date = date(2019, 1, 1),
    baseline_to:   date = date(2021, 12, 31),
    max_workers: int = 6,
) -> tuple[pd.DataFrame, list[dict]]:
    """
    Run all extractors in parallel, merge results, return daily data + coverage.

    Args:
        from_date     : start of the period of interest (default: 12 months ago)
        to_date       : end of the period of interest (default: yesterday)
        baseline_from : start of baseline period
        baseline_to   : end of baseline period
        max_workers   : parallel extractor threads

    Returns:
        (df, coverage)
        df       : daily [date, country, twh, source, provisional, method]
        coverage : list of per-country dicts for appendix/footer
    """
    if to_date is None:
        to_date = date.today() - timedelta(days=1)
    if from_date is None:
        from_date = date(to_date.year - 1, to_date.month, 1)

    logger.info(f"Pipeline run: {from_date} → {to_date}")
    logger.info(f"Baseline:     {baseline_from} → {baseline_to}")

    extractors = _build_extractors()

    # Fetch recent data + baseline data in parallel
    all_dfs = []

    def fetch_one(ext, f, t):
        return ext.fetch(f, t)

    # Try to load baseline from cache first (avoids re-fetching 2019-2021 each run)
    baseline_df = _load_baseline_cache()
    baseline_needed = baseline_from < from_date and baseline_df is None

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        # Recent period
        recent_futures = {
            pool.submit(fetch_one, ext, from_date, to_date): ext
            for ext in extractors
        }
        # Baseline period — only if cache miss and date ranges don't overlap
        if baseline_needed:
            baseline_futures = {
                pool.submit(fetch_one, ext, baseline_from, baseline_to): ext
                for ext in extractors
            }
        else:
            baseline_futures = {}

        for future, ext in recent_futures.items():
            try:
                df = future.result(timeout=120)
                if not df.empty:
                    all_dfs.append(df)
            except Exception as e:
                logger.warning(f"{ext.source} timed out or failed: {e}")

        baseline_parts = []
        for future, ext in baseline_futures.items():
            try:
                df = future.result(timeout=120)
                if not df.empty:
                    all_dfs.append(df)
                    baseline_parts.append(df)
            except Exception as e:
                logger.warning(f"{ext.source} baseline failed: {e}")

        if baseline_parts:
            _save_baseline_cache(pd.concat(baseline_parts, ignore_index=True))

    if baseline_df is not None:
        logger.info("Loaded baseline from cache (skipping re-fetch of 2019-2021).")
        all_dfs.append(baseline_df)

    if not all_dfs:
        logger.error("No data retrieved from any extractor.")
        return pd.DataFrame(), []

    combined = pd.concat(all_dfs, ignore_index=True)

    # De-duplicate: if same country+date appears twice (e.g. ENTSOG + national),
    # prefer the national/direct source over flow-derived
    combined["method_rank"] = combined["method"].map({"direct": 0, "flow_derived": 1}).fillna(2)
    combined = (
        combined
        .sort_values("method_rank")
        .drop_duplicates(subset=["date", "country"], keep="first")
        .drop(columns="method_rank")
    )

    # Split into recent and baseline
    recent_df   = combined[combined["date"] >= from_date].copy()
    coverage    = build_coverage(recent_df, from_date, to_date)

    logger.info(f"Pipeline complete: {len(recent_df)} rows across "
                f"{recent_df['country'].nunique()} countries")

    # Log coverage summary
    for c in coverage:
        status = "✓" if c["complete"] else f"⚠ {c['days_missing']}d missing"
        prov   = f"  ({c['provisional']} provisional)" if c["provisional"] else ""
        logger.info(f"  {c['country']:4s}  last={c['last_date']}  {status}{prov}")

    return combined, coverage


if __name__ == "__main__":
    df, coverage = run()
    print(df.groupby("country")[["twh"]].sum().sort_values("twh", ascending=False))
