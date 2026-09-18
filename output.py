"""
Output module.

Functions:
    save_csv(df, coverage, out_dir)  — writes data + coverage CSVs
    build_pptx(df, coverage, path)   — generates the two-chart PPTX slide
"""

import os
from datetime import date
from pathlib import Path

import pandas as pd

# ── Palette ───────────────────────────────────────────────────────────────────
COUNTRY_COLORS = {
    "DE": "4E9AF1",
    "FR": "7EC8A0",
    "GB": "F06070",
    "IT": "C8A055",
    "ES": "A07ACC",
    "NL": "5BC8C8",
    "Other": "778899",
}

COUNTRY_NAMES = {
    "DE": "Germany", "FR": "France", "GB": "UK",
    "IT": "Italy",   "ES": "Spain",  "NL": "Netherlands",
    "CZ": "Czechia", "AT": "Austria","PL": "Poland",
    "BE": "Belgium", "DK": "Denmark",
    "SK": "Slovakia","LV": "Latvia", "LT": "Lithuania",
    "EE": "Estonia", "FI": "Finland","SE": "Sweden",
    "HU": "Hungary", "RO": "Romania","SI": "Slovenia",
    "HR": "Croatia", "BG": "Bulgaria","GR": "Greece",
    "PT": "Portugal",
}

# Countries shown individually in the chart; rest → "Other"
MAJOR = ["DE", "FR", "GB", "IT", "ES", "NL"]

AMBER = "F0A020"
GREEN = "3A9A70"
MUTED = "7A96B0"
TEXT  = "1A2A3A"
SURF  = "F4F7FB"
GRID  = "DCE8F0"


# ── CSV output ────────────────────────────────────────────────────────────────
def save_csv(df: pd.DataFrame, coverage: list[dict], out_dir: str = "output") -> None:
    Path(out_dir).mkdir(exist_ok=True)

    # Daily data
    data_path = os.path.join(out_dir, "eu_gas_demand_daily.csv")
    df.to_csv(data_path, index=False)

    # Monthly aggregated
    monthly = df.copy()
    monthly["year_month"] = monthly["date"].apply(lambda d: d.strftime("%Y-%m"))
    monthly_agg = (
        monthly.groupby(["year_month", "country"])
        .agg(twh=("twh", "sum"), provisional=("provisional", "any"))
        .reset_index()
    )
    monthly_path = os.path.join(out_dir, "eu_gas_demand_monthly.csv")
    monthly_agg.to_csv(monthly_path, index=False)

    # Coverage report
    cov_path = os.path.join(out_dir, "coverage_report.csv")
    pd.DataFrame(coverage).to_csv(cov_path, index=False)

    print(f"Saved: {data_path}")
    print(f"Saved: {monthly_path}")
    print(f"Saved: {cov_path}")


# ── Chart data preparation ────────────────────────────────────────────────────
def _prep_chart_data(
    df: pd.DataFrame,
    from_date: date,
    to_date: date,
) -> tuple[list, list, list, list, list, list]:
    """
    Returns:
        months          : list of "Mon YY" labels
        country_series  : {country: [twh per month]} for stacked chart
        abs_dev         : list of float (TWh vs baseline)
        pct_dev         : list of float (% vs baseline)
        provisional_months : set of month indices that are provisional
        coverage_notes  : list of strings for footnote
    """
    # Monthly aggregation
    df = df.copy()
    df["ym"] = df["date"].apply(lambda d: (d.year, d.month))

    # Filter to requested window
    df_window = df[
        (df["date"] >= from_date) & (df["date"] <= to_date)
    ]

    # Get sorted list of year-month tuples in window
    months_ym = sorted(df_window["ym"].unique())

    # Group non-major countries into "Other"
    def map_country(c):
        return c if c in MAJOR else "Other"

    df_window = df_window.copy()
    df_window["display_country"] = df_window["country"].map(map_country)

    monthly = (
        df_window.groupby(["ym", "display_country"])
        .agg(twh=("twh", "sum"), provisional=("provisional", "any"))
        .reset_index()
    )

    # Baseline (2019-2021)
    df_baseline = df[
        df["date"].apply(lambda d: 2019 <= d.year <= 2021)
    ].copy()
    df_baseline["display_country"] = df_baseline["country"].map(map_country)
    df_baseline["month"] = df_baseline["date"].apply(lambda d: d.month)

    baseline_avg = (
        df_baseline.groupby(["display_country", "month", df_baseline["date"].apply(lambda d: d.year)])["twh"]
        .sum().reset_index()
        .groupby(["display_country", "month"])["twh"]
        .mean().reset_index()
        .rename(columns={"twh": "twh_baseline"})
    )

    # Build month labels
    import calendar
    month_labels = [
        f"{calendar.month_abbr[ym[1]]} {str(ym[0])[2:]}"
        for ym in months_ym
    ]

    # Build country series
    display_countries = MAJOR + ["Other"]
    country_series = {c: [] for c in display_countries}
    provisional_months = set()
    abs_dev = []
    pct_dev = []

    for i, ym in enumerate(months_ym):
        m_data = monthly[monthly["ym"] == ym]
        month_num = ym[1]
        total_actual = 0
        total_baseline = 0

        for c in display_countries:
            row = m_data[m_data["display_country"] == c]
            val = float(row["twh"].sum()) if not row.empty else 0.0
            country_series[c].append(round(val, 2))
            if not row.empty and bool(row["provisional"].any()):
                provisional_months.add(i)
            total_actual += val

            # Baseline
            b_row = baseline_avg[
                (baseline_avg["display_country"] == c) &
                (baseline_avg["month"] == month_num)
            ]
            total_baseline += float(b_row["twh_baseline"].sum()) if not b_row.empty else 0.0

        abs_dev.append(round(total_actual - total_baseline, 1))
        pct_dev.append(
            round((total_actual - total_baseline) / total_baseline * 100, 1)
            if total_baseline > 0 else 0.0
        )

    return month_labels, country_series, abs_dev, pct_dev, provisional_months


# ── PPTX builder ─────────────────────────────────────────────────────────────
def build_pptx(
    df: pd.DataFrame,
    coverage: list[dict],
    path: str = "output/eu_gas_demand.pptx",
    from_date: date | None = None,
    to_date:   date | None = None,
    white_bg:  bool = True,
) -> str:
    """
    Build the two-chart PPTX slide via pptxgenjs (Node.js library).
    Serialises chart data to JSON, then calls: node generate_pptx.js data.json
    Requires pptxgenjs installed: npm install pptxgenjs  (run once in this folder)
    """
    import json
    import subprocess
    import tempfile

    if to_date is None:
        to_date = df["date"].max()
    if from_date is None:
        from_date = date(to_date.year - 1, to_date.month, 1)

    month_labels, country_series, abs_dev, pct_dev, provisional_months = \
        _prep_chart_data(df, from_date, to_date)

    if not month_labels:
        raise ValueError("No data in the requested window.")

    Path(path).parent.mkdir(exist_ok=True)

    run_date_str = date.today().strftime("%d %b %Y")
    period_str   = (f"{month_labels[0]} – {month_labels[-1]}"
                    if month_labels else "")
    sources_list = sorted({c["source"] for c in coverage})

    payload = {
        "outputPath":       str(Path(path).resolve()),
        "white_bg":         white_bg,
        "run_date":         run_date_str,
        "period_str":       period_str,
        "month_labels":     month_labels,
        "country_series":   country_series,
        "abs_dev":          abs_dev,
        "pct_dev":          pct_dev,
        "provisional_count": len(provisional_months),
        "sources":          sources_list,
        "MAJOR":            MAJOR,
        "COUNTRY_COLORS":   COUNTRY_COLORS,
        "COUNTRY_NAMES":    COUNTRY_NAMES,
    }

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as tmp:
        json.dump(payload, tmp)
        tmp_path = tmp.name

    script = Path(__file__).parent / "generate_pptx.js"
    try:
        result = subprocess.run(
            ["node", str(script), tmp_path],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    if result.returncode != 0:
        raise RuntimeError(
            f"generate_pptx.js failed (exit {result.returncode}):\n"
            f"{result.stderr.strip() or result.stdout.strip()}"
        )

    print(f"Saved: {path}")
    return path
