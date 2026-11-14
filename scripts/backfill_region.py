#!/usr/bin/env python3
"""
Backfill `targets.region` for qualified ES targets (deterministic, coordinate-free).

PRETOR carries `province` for ~100% of qualified Spanish targets, but `region`
(autonomous community) was never populated. Province -> autonomous community is a
hard, public 1:1 fact, so this is a zero-API, zero-hallucination enrichment that
unblocks the region-dependent screening criteria (`repower_permit_headroom`, and
strengthens `irradiation_yield` / `regulatory_clawback`).

Single source of truth: `PROVINCE_TO_REGION` in `src/enrichment/regional_registry.py`.
This script imports that map rather than re-declaring it, so the vocabulary stays
authoritative.

Accuracy guard: if any distinct DB province is missing from the map, the script
prints it and exits non-zero WITHOUT writing. It never guesses a region.

Usage:
    python scripts/backfill_region.py [--dry-run] [--verbose]

Options:
    --dry-run    Show per-province row counts and target regions; write nothing.
    --verbose    Debug logging.
"""

import argparse
import logging
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db import get_db, engine
from src.enrichment.regional_registry import PROVINCE_TO_REGION

console = Console()
logger = logging.getLogger(__name__)


def load_province_counts(conn) -> dict[str, int]:
    """Distinct ES provinces (normalized) -> count of rows with region still NULL."""
    rows = conn.execute(text("""
        SELECT UPPER(TRIM(province)) AS province, COUNT(*) AS n
        FROM targets
        WHERE country = 'ES'
          AND region IS NULL
          AND province IS NOT NULL
        GROUP BY UPPER(TRIM(province))
        ORDER BY n DESC
    """))
    return {r.province: r.n for r in rows}


def main():
    parser = argparse.ArgumentParser(description='Backfill targets.region from province (ES)')
    parser.add_argument('--dry-run', action='store_true', help='Plan only; write nothing')
    parser.add_argument('--verbose', '-v', action='store_true', help='Debug logging')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    )

    console.print("[bold cyan]Region Backfill (deterministic, province -> autonomous community)[/bold cyan]\n")

    # Step 1: read the provinces still needing a region.
    with engine.connect() as conn:
        province_counts = load_province_counts(conn)

    if not province_counts:
        console.print("[yellow]No ES rows with NULL region. Nothing to do.[/yellow]")
        sys.exit(0)

    # Step 2: accuracy guard — every province must be in the authoritative map.
    unmapped = sorted(p for p in province_counts if p not in PROVINCE_TO_REGION)
    if unmapped:
        console.print("[bold red]Unmapped provinces found — refusing to write:[/bold red]")
        for p in unmapped:
            console.print(f"  [red]{p}[/red] ({province_counts[p]} rows)")
        console.print(
            "\n[red]Add these to PROVINCE_TO_REGION in "
            "src/enrichment/regional_registry.py before running.[/red]"
        )
        sys.exit(1)

    # Step 3: show the plan.
    plan = Table(title="Backfill Plan")
    plan.add_column("Province", style="cyan")
    plan.add_column("Region", style="magenta")
    plan.add_column("Rows", style="green", justify="right")
    total = 0
    for province, n in province_counts.items():
        plan.add_row(province, PROVINCE_TO_REGION[province], str(n))
        total += n
    console.print(plan)
    console.print(
        f"\n[dim]{len(province_counts)} provinces, all mapped, "
        f"{total} rows to fill.[/dim]\n"
    )

    if args.dry_run:
        console.print("[yellow]Dry run — database not modified.[/yellow]")
        sys.exit(0)

    # Step 4: idempotent UPDATE, one statement per province.
    console.print("[cyan]Updating database...[/cyan]")
    updated = 0
    with get_db() as db:
        for province, region in PROVINCE_TO_REGION.items():
            result = db.execute(
                text("""
                    UPDATE targets
                       SET region = :region,
                           updated_at = NOW()
                     WHERE country = 'ES'
                       AND region IS NULL
                       AND UPPER(TRIM(province)) = :province
                """),
                {'region': region, 'province': province},
            )
            updated += result.rowcount or 0

    console.print(f"[green]Updated {updated} targets with region.[/green]\n")

    # Step 5: coverage summary.
    with engine.connect() as conn:
        total_es = conn.execute(text(
            "SELECT COUNT(*) FROM targets WHERE country = 'ES'"
        )).scalar()
        with_region = conn.execute(text(
            "SELECT COUNT(*) FROM targets WHERE country = 'ES' AND region IS NOT NULL"
        )).scalar()
        still_null = conn.execute(text(
            "SELECT COUNT(*) FROM targets WHERE country = 'ES' AND region IS NULL"
        )).scalar()

    summary = Table(title="Region Coverage")
    summary.add_column("Metric", style="cyan")
    summary.add_column("Value", style="green")
    summary.add_row("Total ES targets", str(total_es))
    summary.add_row("With region", str(with_region))
    summary.add_row("Coverage", f"{100 * with_region / total_es:.1f}%" if total_es else "0%")
    summary.add_row("Remaining NULL", str(still_null))
    console.print(summary)

    if still_null:
        console.print(f"\n[yellow]Warning: {still_null} ES rows still have NULL region "
                      "(likely NULL province).[/yellow]")
    else:
        console.print("\n[bold green]Region backfill complete — 100% coverage.[/bold green]")
    console.print("[dim]Re-score to measure the confidence lift: "
                  "python scripts/run_scoring.py --stage screening --top 5[/dim]")


if __name__ == '__main__':
    main()
