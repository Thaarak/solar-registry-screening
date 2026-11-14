#!/usr/bin/env python3
"""
Run SEC cadastral lookups on targets that have coordinates.

Usage:
    python scripts/run_cadastral_lookups.py [options]

Options:
    --limit N       Process only first N targets
    --dry-run       Show targets but don't make API calls
    --verbose       Enable debug logging
"""

import argparse
import logging
import sys
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.table import Table
from sqlalchemy import text

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db import get_db, engine
from src.enrichment.sec_cadastral import SECCadastralClient

console = Console()
logger = logging.getLogger(__name__)


def get_targets_with_coordinates(db, limit: int = None):
    """Get targets that have coordinates but no cadastral record."""
    query = """
        SELECT t.id, t.plant_name, t.latitude, t.longitude, t.province
        FROM targets t
        LEFT JOIN cadastral_records cr ON t.id = cr.target_id
        WHERE t.country = 'ES'
          AND t.latitude IS NOT NULL
          AND t.longitude IS NOT NULL
          AND cr.id IS NULL
        ORDER BY t.capacity_kw DESC
    """
    if limit:
        query += f" LIMIT {limit}"

    result = db.execute(text(query))

    targets = []
    for row in result:
        targets.append({
            'id': str(row.id),
            'plant_name': row.plant_name,
            'latitude': float(row.latitude),
            'longitude': float(row.longitude),
            'province': row.province,
        })

    return targets


def insert_cadastral_record(db, target_id: str, ref_catastral: str, method: str = 'SEC_WMS'):
    """Insert a cadastral record."""
    db.execute(
        text("""
            INSERT INTO cadastral_records (
                target_id, referencia_catastral, lookup_method, lookup_success
            ) VALUES (
                :target_id, :ref_catastral, :method, TRUE
            )
            ON CONFLICT (target_id)
            DO UPDATE SET
                referencia_catastral = EXCLUDED.referencia_catastral,
                lookup_method = EXCLUDED.lookup_method,
                lookup_date = NOW()
        """),
        {
            'target_id': target_id,
            'ref_catastral': ref_catastral,
            'method': method,
        }
    )


def main():
    parser = argparse.ArgumentParser(
        description='Run SEC cadastral lookups on targets with coordinates'
    )
    parser.add_argument('--limit', type=int, help='Limit number of targets')
    parser.add_argument('--dry-run', action='store_true', help='Show targets without API calls')
    parser.add_argument('--verbose', '-v', action='store_true', help='Debug logging')

    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    console.print("[bold cyan]SEC Cadastral Lookups[/bold cyan]\n")

    # Get targets with coordinates
    console.print("[cyan]Loading targets with coordinates...[/cyan]")

    with get_db() as db:
        targets = get_targets_with_coordinates(db, args.limit)

    console.print(f"[green]Found {len(targets)} targets needing cadastral lookup[/green]\n")

    if not targets:
        console.print("[yellow]No targets need cadastral lookup![/yellow]")
        sys.exit(0)

    # Show sample
    console.print("[cyan]Sample targets:[/cyan]")
    for t in targets[:5]:
        console.print(f"  - {t['plant_name']}: ({t['latitude']:.4f}, {t['longitude']:.4f}) - {t['province']}")
    if len(targets) > 5:
        console.print(f"  ... and {len(targets) - 5} more")
    console.print()

    if args.dry_run:
        console.print("[yellow]Dry run - not making API calls[/yellow]")
        sys.exit(0)

    # Run cadastral lookups
    console.print("[cyan]Running SEC cadastral lookups...[/cyan]")
    console.print("[dim]Rate limited to 0.5s between requests...[/dim]\n")

    sec_client = SECCadastralClient(delay_seconds=0.5)
    success_count = 0
    failed_count = 0

    with get_db() as db:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Looking up...", total=len(targets))

            for target in targets:
                result = sec_client.lookup_coordinates(
                    target['latitude'],
                    target['longitude']
                )

                if result.success and result.referencia_catastral:
                    insert_cadastral_record(
                        db,
                        target['id'],
                        result.referencia_catastral,
                        'SEC_WMS'
                    )
                    success_count += 1
                    logger.debug(
                        f"Found: {target['plant_name']} -> {result.referencia_catastral}"
                    )
                else:
                    failed_count += 1
                    logger.debug(
                        f"No result: {target['plant_name']} - {result.error}"
                    )

                progress.advance(task)

    # Show results
    console.print()

    results_table = Table(title="Cadastral Lookup Results")
    results_table.add_column("Metric", style="cyan")
    results_table.add_column("Value", style="green")

    results_table.add_row("Targets processed", str(len(targets)))
    results_table.add_row("Successful lookups", str(success_count))
    results_table.add_row("Failed lookups", str(failed_count))
    results_table.add_row("Success rate", f"{100*success_count/len(targets):.1f}%")

    console.print(results_table)

    # Show SEC stats
    sec_stats = sec_client.get_stats()
    console.print(f"\n[dim]SEC API stats: {sec_stats}[/dim]")

    # Final database summary
    with engine.connect() as conn:
        total_targets = conn.execute(text(
            "SELECT COUNT(*) FROM targets WHERE country = 'ES'"
        )).scalar()
        total_cadastral = conn.execute(text("""
            SELECT COUNT(*) FROM cadastral_records cr
            JOIN targets t ON cr.target_id = t.id
            WHERE t.country = 'ES'
        """)).scalar()

    summary = Table(title="Database Summary")
    summary.add_column("Metric", style="cyan")
    summary.add_column("Value", style="green")
    summary.add_row("Total Spanish targets", str(total_targets))
    summary.add_row("With cadastral reference", str(total_cadastral))
    summary.add_row("Coverage", f"{100*total_cadastral/total_targets:.1f}%")

    console.print(summary)

    console.print("\n[bold green]Cadastral lookups complete![/bold green]")


if __name__ == '__main__':
    main()
