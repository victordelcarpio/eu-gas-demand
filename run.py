"""
European Gas Demand Pipeline — main entry point.

Usage:
    python run.py                          # last 12 months, output to ./output/
    python run.py --from 2024-01-01        # custom start date
    python run.py --dark                   # dark background slide
    python run.py --csv-only               # skip PPTX, CSV only
    python run.py --help
"""

import argparse
import logging
from datetime import date, datetime

from pipeline import run
from output import save_csv, build_pptx

logger = logging.getLogger(__name__)


def parse_args():
    p = argparse.ArgumentParser(description="EU gas demand pipeline")
    p.add_argument("--from", dest="from_date", default=None,
                   help="Start date YYYY-MM-DD (default: 12 months ago)")
    p.add_argument("--to", dest="to_date", default=None,
                   help="End date YYYY-MM-DD (default: yesterday)")
    p.add_argument("--out", default="output",
                   help="Output directory (default: ./output)")
    p.add_argument("--dark", action="store_true",
                   help="Dark background PPTX")
    p.add_argument("--csv-only", action="store_true",
                   help="Skip PPTX generation")
    p.add_argument("--workers", type=int, default=6,
                   help="Parallel extractor threads (default: 6)")
    return p.parse_args()


def main():
    args = parse_args()

    from_date = (
        datetime.strptime(args.from_date, "%Y-%m-%d").date()
        if args.from_date else None
    )
    to_date = (
        datetime.strptime(args.to_date, "%Y-%m-%d").date()
        if args.to_date else None
    )

    # Run pipeline
    df, coverage = run(
        from_date=from_date,
        to_date=to_date,
        max_workers=args.workers,
    )

    if df.empty:
        print("No data retrieved. Check network access and extractor logs.")
        return

    # Save CSVs
    save_csv(df, coverage, out_dir=args.out)

    # Build PPTX
    if not args.csv_only:
        pptx_path = f"{args.out}/eu_gas_demand.pptx"
        build_pptx(
            df, coverage,
            path=pptx_path,
            from_date=from_date,
            to_date=to_date,
            white_bg=not args.dark,
        )

    # Print coverage summary to terminal
    print("\n── Coverage summary ─────────────────────────────────────────────")
    print(f"{'Country':<8} {'Source':<40} {'Last date':<12} {'Missing':>8} {'Prov':>6}")
    print("─" * 80)
    for c in coverage:
        flag = "⚠" if c["days_missing"] > 0 else "✓"
        print(
            f"{flag} {c['country']:<6} {c['source'][:38]:<40} "
            f"{str(c['last_date']):<12} {c['days_missing']:>6}d  {c['provisional']:>5}"
        )


if __name__ == "__main__":
    main()
