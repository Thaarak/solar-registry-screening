#!/usr/bin/env python3
"""
Backfill targets.regional_registry_id from the raw PRETOR .xls files.

The PRETOR exports carry a "Número de Registro Autonómico Definitivo" (col 5),
keyed by "Clave Registro" (col 0, e.g. RE-000006). This is the deterministic
join key into the regional installation registries. Existing rows were loaded
before we parsed it, so this one-off backfill reads the raw files and updates
targets in place by source_id — no full reload needed.

Usage:
    python scripts/backfill_regional_id.py [options]

Options:
    --dry-run   Show what would be updated without writing
    --verbose   Debug logging
"""

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd
from rich.console import Console
from rich.table import Table
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db import engine, get_db
from src.scrapers.pretor import parse_regional_id

console = Console()
logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / 'data'
RAW_FILES = [
    DATA_DIR / 'InformeInstalaciones-11_06_2026.xls',
    DATA_DIR / 'region_2.xls',
    DATA_DIR / 'region_3.xls',
    DATA_DIR / 'region_4.xls',
    DATA_DIR / 'region_5.xls',
]

CLAVE_COL = 'Clave Registro'
REGIONAL_COL = 'Número de Registro Autonómico Definitivo'


def build_id_map() -> dict[str, str]:
    """Map source_id (Clave Registro) -> regional_registry_id from raw files."""
    id_map: dict[str, str] = {}
    for f in RAW_FILES:
        if not f.exists():
            logger.warning(f"Missing raw file: {f}")
            continue

        df = pd.read_html(str(f), encoding='iso-8859-1', header=0)[0]
        df.columns = [str(c).strip() for c in df.columns]

        if CLAVE_COL not in df.columns or REGIONAL_COL not in df.columns:
            logger.warning(f"{f.name}: expected columns not found, skipping")
            continue

        for _, row in df.iterrows():
            source_id = str(row[CLAVE_COL]).strip()
            if not source_id or source_id.upper() == 'NAN':
                continue
            regional_id = parse_regional_id(row[REGIONAL_COL])
            if regional_id:
                id_map[source_id[:100]] = regional_id

        console.print(f"[dim]{f.name}: {len(df)} rows[/dim]")

    return id_map


def main():
    parser = argparse.ArgumentParser(description='Backfill regional_registry_id on targets')
    parser.add_argument('--dry-run', action='store_true', help='Show without writing')
    parser.add_argument('--verbose', '-v', action='store_true', help='Debug logging')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    )

    console.print("[bold cyan]Backfill regional_registry_id[/bold cyan]\n")

    console.print("[cyan]Step 1: Reading raw PRETOR files...[/cyan]")
    id_map = build_id_map()
    console.print(f"[green]Collected {len(id_map)} regional ids from raw files[/green]\n")

    if not id_map:
        console.print("[yellow]No regional ids found - nothing to do[/yellow]")
        sys.exit(0)

    # Load existing PRETOR targets (source_id) that still lack a regional id
    console.print("[cyan]Step 2: Matching against targets table...[/cyan]")
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT id, source_id
            FROM targets
            WHERE country = 'ES' AND source_registry = 'PRETOR'
        """)).fetchall()

    total_targets = len(rows)
    updates = []
    for r in rows:
        rid = id_map.get(r.source_id)
        if rid:
            updates.append({'target_id': r.id, 'rid': rid})

    console.print(
        f"[green]{len(updates)} of {total_targets} PRETOR targets have a regional id "
        f"({100 * len(updates) / total_targets:.1f}%)[/green]\n"
    )

    if args.dry_run:
        console.print("[yellow]Dry run - not writing[/yellow]")
        sample = Table(title="Sample updates")
        sample.add_column("source_id (target)", style="cyan")
        sample.add_column("regional_registry_id", style="green")
        for r in rows[:10]:
            rid = id_map.get(r.source_id)
            if rid:
                sample.add_row(r.source_id, rid)
        console.print(sample)
        sys.exit(0)

    console.print("[cyan]Step 3: Updating database...[/cyan]")
    updated = 0
    with get_db() as db:
        for u in updates:
            db.execute(
                text("UPDATE targets SET regional_registry_id = :rid, updated_at = NOW() WHERE id = :target_id"),
                u,
            )
            updated += 1

    console.print(f"[green]Updated {updated} targets[/green]\n")

    with engine.connect() as conn:
        with_id = conn.execute(text("""
            SELECT COUNT(*) FROM targets
            WHERE country = 'ES' AND source_registry = 'PRETOR'
              AND regional_registry_id IS NOT NULL
        """)).scalar()

    summary = Table(title="Regional ID Coverage (PRETOR targets)")
    summary.add_column("Metric", style="cyan")
    summary.add_column("Value", style="green")
    summary.add_row("Total PRETOR targets", str(total_targets))
    summary.add_row("With regional id", str(with_id))
    summary.add_row("Coverage", f"{100 * with_id / total_targets:.1f}%")
    console.print(summary)


if __name__ == '__main__':
    main()
