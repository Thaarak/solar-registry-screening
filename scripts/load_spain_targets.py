#!/usr/bin/env python3
"""
Load Spanish solar plant targets from PRETOR registry to database.

Usage:
    python scripts/load_spain_targets.py <pretor_file.xls> [options]

Options:
    --dry-run           Parse file but don't write to database
    --skip-cadastral    Skip SEC cadastral lookup
    --limit N           Process only first N records
    --verbose           Enable debug logging
"""

import argparse
import logging
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.table import Table
from sqlalchemy import text

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db import get_db, test_connection, engine
from src.scrapers.pretor import PretorParser
from src.enrichment.sec_cadastral import SECCadastralClient
from src.models import TargetCreate

console = Console()
logger = logging.getLogger(__name__)


def insert_target(db, target: TargetCreate) -> str | None:
    """Upsert a target and return its database ID."""
    result = db.execute(
        text("""
            INSERT INTO targets (
                country, source_registry, source_id, regional_registry_id,
                plant_name, capacity_kw, commissioning_date, technology,
                latitude, longitude, municipality, province, region,
                tariff_regime, is_operating, is_qualified,
                data_pulled_at
            ) VALUES (
                :country, :source_registry, :source_id, :regional_registry_id,
                :plant_name, :capacity_kw, :commissioning_date, :technology,
                :latitude, :longitude, :municipality, :province, :region,
                :tariff_regime, :is_operating, :is_qualified,
                :data_pulled_at
            )
            ON CONFLICT (country, source_registry, source_id)
            DO UPDATE SET
                plant_name = EXCLUDED.plant_name,
                capacity_kw = EXCLUDED.capacity_kw,
                regional_registry_id = COALESCE(EXCLUDED.regional_registry_id, targets.regional_registry_id),
                updated_at = NOW()
            RETURNING id
        """),
        {
            'country': target.country.value,
            'source_registry': target.source_registry,
            'source_id': target.source_id,
            'regional_registry_id': target.regional_registry_id,
            'plant_name': target.plant_name,
            'capacity_kw': float(target.capacity_kw),
            'commissioning_date': target.commissioning_date,
            'technology': target.technology,
            'latitude': float(target.latitude) if target.latitude is not None else None,
            'longitude': float(target.longitude) if target.longitude is not None else None,
            'municipality': target.municipality,
            'province': target.province,
            'region': target.region,
            'tariff_regime': target.tariff_regime.value,
            'is_operating': target.is_operating,
            'is_qualified': target.is_qualified,
            'data_pulled_at': datetime.utcnow(),
        }
    )
    row = result.fetchone()
    return str(row[0]) if row else None


def insert_cadastral_record(db, target_id: str, ref_catastral: str, method: str = 'SEC_WMS') -> bool:
    """Insert a cadastral record for a target."""
    try:
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
        return True
    except Exception as e:
        logger.error(f"Failed to insert cadastral record for {target_id}: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description='Load Spanish PRETOR targets to database'
    )
    parser.add_argument('file', type=Path, help='PRETOR CSV/XLS/XLSX file')
    parser.add_argument('--dry-run', action='store_true', help='Parse only, no DB write')
    parser.add_argument('--skip-cadastral', action='store_true', help='Skip cadastral lookup')
    parser.add_argument('--limit', type=int, help='Limit number of records')
    parser.add_argument('--verbose', '-v', action='store_true', help='Debug logging')

    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # Validate file
    if not args.file.exists():
        console.print(f"[red]File not found: {args.file}[/red]")
        sys.exit(1)

    # Test database connection
    if not args.dry_run:
        console.print("[cyan]Testing database connection...[/cyan]")
        if not test_connection():
            console.print("[red]Database connection failed![/red]")
            console.print("[yellow]Make sure PostgreSQL is running: docker-compose up -d[/yellow]")
            sys.exit(1)
        console.print("[green]Database connected![/green]\n")

    # Parse PRETOR file
    console.print(f"[cyan]Parsing PRETOR file: {args.file}[/cyan]")

    pretor_parser = PretorParser()
    targets = list(pretor_parser.parse_file(args.file))

    if args.limit:
        targets = targets[:args.limit]

    # Show parsing stats
    stats = pretor_parser.get_stats()
    stats_table = Table(title="Parsing Statistics")
    stats_table.add_column("Metric", style="cyan")
    stats_table.add_column("Value", style="green")

    stats_table.add_row("Total rows in file", str(stats['total_rows']))
    stats_table.add_row("Parsed (passed filters)", str(stats['parsed']))
    stats_table.add_row("Filtered: capacity", str(stats['filtered_capacity']))
    stats_table.add_row("Filtered: date", str(stats['filtered_date']))
    stats_table.add_row("Filtered: technology", str(stats['filtered_technology']))
    stats_table.add_row("Filtered: status", str(stats['filtered_status']))
    stats_table.add_row("Validation errors", str(stats['validation_errors']))

    console.print(stats_table)
    console.print()

    if not targets:
        console.print("[yellow]No qualified targets found![/yellow]")
        sys.exit(0)

    console.print(f"[green]Found {len(targets)} qualified targets[/green]\n")

    if args.dry_run:
        console.print("[yellow]Dry run - not writing to database[/yellow]")

        # Show sample
        console.print("\n[cyan]Sample targets:[/cyan]")
        for t in targets[:5]:
            console.print(f"  - {t.plant_name or t.source_id}: {t.capacity_kw} kW, {t.province}")
        sys.exit(0)

    # Insert targets to database
    console.print("[cyan]Inserting targets to database...[/cyan]")

    inserted_ids = []
    with get_db() as db:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Inserting targets...", total=len(targets))

            for target in targets:
                target_id = insert_target(db, target)
                if target_id:
                    inserted_ids.append((target_id, target))
                progress.advance(task)

    console.print(f"[green]Inserted {len(inserted_ids)} targets[/green]\n")

    # Cadastral lookup
    if not args.skip_cadastral and inserted_ids:
        # Filter to targets with coordinates
        targets_with_coords = [
            (tid, t) for tid, t in inserted_ids
            if t.latitude and t.longitude
        ]

        if targets_with_coords:
            console.print(f"[cyan]Looking up cadastral references for {len(targets_with_coords)} targets...[/cyan]")

            sec_client = SECCadastralClient(delay_seconds=0.5)
            cadastral_success = 0

            with get_db() as db:
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(),
                    TaskProgressColumn(),
                    console=console,
                ) as progress:
                    task = progress.add_task("SEC lookups...", total=len(targets_with_coords))

                    for target_id, target in targets_with_coords:
                        result = sec_client.lookup_coordinates(
                            target.latitude,
                            target.longitude
                        )

                        if result.success and result.referencia_catastral:
                            if insert_cadastral_record(db, target_id, result.referencia_catastral):
                                cadastral_success += 1

                        progress.advance(task)

            console.print(f"[green]Added {cadastral_success} cadastral references[/green]")

            sec_stats = sec_client.get_stats()
            console.print(f"[dim]SEC API stats: {sec_stats}[/dim]\n")
        else:
            console.print("[yellow]No targets have coordinates for cadastral lookup[/yellow]\n")

    # Final summary
    console.print("[bold green]Load complete![/bold green]")

    # Show database counts
    with engine.connect() as conn:
        targets_count = conn.execute(text("SELECT COUNT(*) FROM targets WHERE country = 'ES'")).scalar()
        cadastral_count = conn.execute(text("""
            SELECT COUNT(*) FROM cadastral_records cr
            JOIN targets t ON cr.target_id = t.id
            WHERE t.country = 'ES'
        """)).scalar()

    summary = Table(title="Database Summary (Spain)")
    summary.add_column("Table", style="cyan")
    summary.add_column("Count", style="green")
    summary.add_row("Targets", str(targets_count))
    summary.add_row("Cadastral Records", str(cadastral_count))
    console.print(summary)


if __name__ == '__main__':
    main()
