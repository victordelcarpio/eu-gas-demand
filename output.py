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
    try:
        pptx = __import__("pptxgenjs")
    except ImportError:
        raise ImportError("pptxgenjs not found — run: npm install pptxgenjs")

    if to_date is None:
        to_date = df["date"].max()
    if from_date is None:
        from_date = date(to_date.year - 1, to_date.month, 1)

    month_labels, country_series, abs_dev, pct_dev, provisional_months = \
        _prep_chart_data(df, from_date, to_date)

    if not month_labels:
        raise ValueError("No data in the requested window.")

    Path(path).parent.mkdir(exist_ok=True)

    bg   = "FFFFFF" if white_bg else "0F1823"
    surf = "F4F7FB" if white_bg else "151F2E"
    grid = "DCE8F0" if white_bg else "1E2E42"
    text = "1A2A3A" if white_bg else "C8D8E8"
    muted= "7A96B0"

    pres = pptx.create()
    pres.layout = "LAYOUT_WIDE"

    slide = pres.addSlide()

    # Background
    slide.addShape(pres.ShapeType.rect, {
        "x": 0, "y": 0, "w": "100%", "h": "100%",
        "fill": {"color": bg},
    })

    # Run date and provisional note
    run_date_str = date.today().strftime("%d %b %Y")
    period_str   = f"{month_labels[0]} – {month_labels[-1]}"
    prov_note    = (f"  ·  {len(provisional_months)} month(s) provisional"
                    if provisional_months else "")

    slide.addText("European Natural Gas Demand Monitor", {
        "x": 0.4, "y": 0.22, "w": 12, "h": 0.35,
        "fontSize": 16, "bold": True, "color": text,
        "fontFace": "Calibri", "isTextBox": True, "margin": 0,
    })

    slide.addText(
        f"Last 12 months  ·  {period_str}  ·  Data as of {run_date_str}{prov_note}",
        {
            "x": 0.4, "y": 0.58, "w": 12, "h": 0.22,
            "fontSize": 9, "color": muted, "fontFace": "Calibri",
            "isTextBox": True, "margin": 0,
        }
    )

    # ── Chart 1: Stacked column ───────────────────────────────────────────────
    display_countries = MAJOR + ["Other"]
    chart1_data = [
        {
            "name":   COUNTRY_NAMES.get(c, c),
            "labels": month_labels,
            "values": country_series[c],
        }
        for c in display_countries
    ]

    slide.addText("Monthly gas consumption by country  (TWh)", {
        "x": 0.4, "y": 0.9, "w": 6, "h": 0.25,
        "fontSize": 10, "bold": True, "color": text,
        "fontFace": "Calibri", "isTextBox": True, "margin": 0,
    })

    slide.addChart(pres.ChartType.bar, chart1_data, {
        "x": 0.4, "y": 1.18, "w": 6.1, "h": 5.5,
        "barDir": "col",
        "barGrouping": "stacked",
        "chartColors": [COUNTRY_COLORS.get(c, "AAAAAA") for c in display_countries],
        "showLegend": True,
        "legendPos": "b",
        "legendFontSize": 8,
        "legendColor": text,
        "showValue": False,
        "catAxisLabelColor": muted,
        "valAxisLabelColor": muted,
        "valAxisLabelFontSize": 8,
        "catAxisLabelFontSize": 8,
        "valGridLine": {"color": grid, "size": 0.5},
        "catGridLine": {"style": "none"},
        "plotArea": {"fill": {"color": surf}},
        "chartArea": {"fill": {"color": bg}, "border": {"color": bg}},
        "showTitle": False,
        "valAxisMinVal": 0,
    })

    # ── Chart 2: Deviation combo ──────────────────────────────────────────────
    slide.addText("Demand deviation vs 2019–21 average  (bars = TWh · line = %)", {
        "x": 6.85, "y": 0.9, "w": 6.1, "h": 0.25,
        "fontSize": 10, "bold": True, "color": text,
        "fontFace": "Calibri", "isTextBox": True, "margin": 0,
    })

    val_min = min(abs_dev) * 1.1 if abs_dev else -120
    pct_min = min(pct_dev) * 1.1 if pct_dev else -30

    slide.addChart(
        [
            {
                "type": pres.ChartType.bar,
                "data": [{"name": "TWh deviation", "labels": month_labels, "values": abs_dev}],
                "options": {"chartColors": [AMBER], "barDir": "col"},
            },
            {
                "type": pres.ChartType.line,
                "data": [{"name": "% vs baseline", "labels": month_labels, "values": pct_dev}],
                "options": {
                    "chartColors": [GREEN],
                    "lineSize": 2,
                    "lineSmooth": True,
                    "secondaryValAxis": True,
                    "secondaryCatAxis": True,
                },
            },
        ],
        {
            "x": 6.85, "y": 1.18, "w": 6.1, "h": 5.5,
            "valAxes": [
                {
                    "showValAxisTitle": False,
                    "valAxisMinVal": val_min,
                    "valAxisMaxVal": 0,
                    "valAxisLabelColor": muted,
                    "valAxisLabelFontSize": 8,
                    "valGridLine": {"color": grid, "size": 0.5},
                },
                {
                    "showValAxisTitle": False,
                    "valAxisMinVal": pct_min,
                    "valAxisMaxVal": 0,
                    "valAxisLabelColor": GREEN,
                    "valAxisLabelFontSize": 8,
                    "valGridLine": {"style": "none"},
                    "valAxisCrossesAt": "autoZero",
                },
            ],
            "catAxes": [
                {"catAxisLabelColor": muted, "catAxisLabelFontSize": 8},
                {"catAxisHide": True},
            ],
            "showLegend": True,
            "legendPos": "b",
            "legendFontSize": 8,
            "legendColor": text,
            "plotArea": {"fill": {"color": surf}},
            "chartArea": {"fill": {"color": bg}, "border": {"color": bg}},
            "showTitle": False,
        }
    )

    # ── Source footnote ───────────────────────────────────────────────────────
    sources = list({c["source"] for c in coverage})
    source_str = "Sources: " + "  ·  ".join(sorted(sources)[:6])
    if provisional_months:
        source_str += "  ·  * Provisional months may be revised"

    slide.addText(source_str, {
        "x": 0.4, "y": 7.15, "w": 12.5, "h": 0.2,
        "fontSize": 7, "color": muted, "fontFace": "Calibri",
        "isTextBox": True, "margin": 0,
    })

    slide.addNotes(
        f"Data as of {run_date_str}. "
        f"Baseline: 2019–2021 monthly average. "
        f"Flow-derived countries (SK, LV, LT, EE, FI, SE): "
        f"consumption estimated from ENTSOG border flow mass balance. "
        f"Other EU: remaining countries aggregated."
    )

    pres.writeFile({"fileName": path})
    print(f"Saved: {path}")
    return path
