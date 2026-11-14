#!/usr/bin/env python3
"""
Enrich qualified ES targets with grid-congestion / curtailment classification.

Bucket-1 *coordinate-free* enrichment (§3.7): every qualified Spanish target is
classified by province / grid zone into a curtailment-risk band sourced from
REE's capacity-access maps. This needs no per-plant lat/long, so it covers
100% of qualified targets and lifts the screening-derivable base — wiring up
the `curtailment` and `export_poi_value` (and stretch `repower_permit_headroom`)
scorecard criteria.

Mirrors `scripts/enrich_regional.py`: load -> classify -> UPDATE with provenance
note -> audit CSV -> rich coverage table. Graceful offline posture: the curated
table guarantees full coverage; an optional ESIOS refresh overlays it when a
token is present.

Usage:
    python scripts/enrich_grid_congestion.py [options]

Options:
    --dry-run                 Classify without updating the database
    --limit N                 Process only first N targets (testing)
    --verbose                 Enable debug logging
"""

import argparse
import csv
import logging
import sys
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.table import Table
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db import get_db, engine
from src.enrichment.grid_congestion import GridCongestionSource

console = Console()
logger = logging.getLogger(__name__)

REPORT_PATH = Path(__file__).parent.parent / 'data' / 'grid_congestion_matches.csv'


def load_targets(db, limit: int = None) -> list[dict]:
    """All qualified ES targets with a province, for congestion classification."""
    query = """
        SELECT id, plant_name, capacity_kw, province, region
        FROM targets
        WHERE country = 'ES'
          AND is_qualified = TRUE
        ORDER BY province, plant_name
    """
    if limit:
        query += f" LIMIT {limit}"

    result = db.execute(text(query))
    return [
        {
            'id': row.id,
            'plant_name': row.plant_name,
            'capacity_kw': float(row.capacity_kw) if row.capacity_kw is not None else None,
            'province': row.province,
            'region': row.region,
        }
        for row in result
    ]


def update_target_congestion(db, target_id, classification: dict):
    """Write classification + provenance to the targets table."""
    db.execute(
        text("""
            UPDATE targets
            SET curtailment_risk = :risk,
                grid_congestion_zone = :zone,
                qualification_notes = COALESCE(qualification_notes, '') ||
                    ' [Grid congestion: ' || :note || ']',
                updated_at = NOW()
            WHERE id = :target_id
        """),
        {
            'risk': classification['curtailment_risk'],
            'zone': classification['grid_zone'],
            'note': f"REE {classification['grid_zone']}/{classification['curtailment_risk']}",
            'target_id': target_id,
        },
    )


def write_report(rows: list[dict]):
    """Write audit CSV of all classifications."""
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(REPORT_PATH, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([
            'target_id', 'plant_name', 'province', 'region',
            'capacity_kw', 'curtailment_risk', 'grid_congestion_zone',
        ])
        for r in rows:
            writer.writerow([
                r['id'], r['plant_name'], r['province'], r['region'],
                r['capacity_kw'], r['curtailment_risk'], r['grid_zone'],
            ])


def main():
    parser = argparse.ArgumentParser(
        description='Enrich qualified ES targets with grid-congestion classification'
    )
    parser.add_argument('--dry-run', action='store_true', help='Classify without updating')
    parser.add_argument('--limit', type=int, help='Process only first N targets')
    parser.add_argument('--verbose', '-v', action='store_true', help='Debug logging')

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    )
    if not args.verbose:
        logging.getLogger('urllib3').setLevel(logging.WARNING)

    console.print("[bold cyan]Grid Congestion Enrichment (Bucket-1, coordinate-free)[/bold cyan]\n")

    # Step 1: load qualified targets
    console.print("[cyan]Step 1: Loading qualified ES targets...[/cyan]")
    with get_db() as db:
        targets = load_targets(db, limit=args.limit)
    console.print(f"[green]Loaded {len(targets)} qualified targets[/green]\n")

    if not targets:
        console.print("[yellow]Nothing to do![/yellow]")
        sys.exit(0)

    # Step 2: classify each target
    console.print("[cyan]Step 2: Classifying by province / grid zone...[/cyan]\n")
    source = GridCongestionSource()
    source.refresh()  # best-effort live overlay; curated table otherwise

    classified: list[dict] = []
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Classifying...", total=len(targets))
        for t in targets:
            cls = source.classify(t['province'])
            classified.append({**t, **cls})
            progress.advance(task)

    # Step 3: results breakdown
    risk_counts: dict[str, int] = {}
    zone_counts: dict[str, int] = {}
    for r in classified:
        risk_counts[r['curtailment_risk']] = risk_counts.get(r['curtailment_risk'], 0) + 1
        zone_counts[r['grid_zone']] = zone_counts.get(r['grid_zone'], 0) + 1

    risk_table = Table(title="Curtailment Risk Distribution")
    risk_table.add_column("Risk", style="cyan")
    risk_table.add_column("Targets", style="green")
    for risk in ('none', 'moderate', 'high'):
        if risk in risk_counts:
            risk_table.add_row(risk, str(risk_counts[risk]))
    for risk, cnt in risk_counts.items():
        if risk not in ('none', 'moderate', 'high'):
            risk_table.add_row(risk, str(cnt))
    console.print(risk_table)
    console.print()

    console.print(f"[dim]Source stats: {source.get_stats()}[/dim]\n")

    write_report(classified)
    console.print(f"[dim]Audit report written to {REPORT_PATH}[/dim]\n")

    if args.dry_run:
        console.print("[yellow]Dry run - not updating database[/yellow]")
        console.print(f"[dim]Would classify {len(classified)} targets[/dim]")
        sys.exit(0)

    # Step 4: update database
    console.print("[cyan]Step 3: Updating database...[/cyan]")
    updated = 0
    with get_db() as db:
        for r in classified:
            try:
                update_target_congestion(db, r['id'], r)
                updated += 1
            except Exception as e:
                logger.error(f"Failed to update {r['id']}: {e}")

    console.print(f"[green]Updated {updated} targets with grid-congestion classification![/green]\n")

    # Final coverage summary
    with engine.connect() as conn:
        total = conn.execute(text(
            "SELECT COUNT(*) FROM targets WHERE country = 'ES' AND is_qualified = TRUE"
        )).scalar()
        with_risk = conn.execute(text(
            "SELECT COUNT(*) FROM targets WHERE country = 'ES' AND is_qualified = TRUE "
            "AND curtailment_risk IS NOT NULL"
        )).scalar()

    summary = Table(title="Grid Congestion Coverage")
    summary.add_column("Metric", style="cyan")
    summary.add_column("Value", style="green")
    summary.add_row("Qualified ES targets", str(total))
    summary.add_row("With curtailment_risk", str(with_risk))
    summary.add_row("Coverage", f"{100 * with_risk / total:.1f}%" if total else "0%")
    console.print(summary)

    console.print("\n[bold green]Grid congestion enrichment complete![/bold green]")
    console.print("[dim]Re-run screening scoring to pick up the new fields: "
                  "python scripts/run_scoring.py --stage screening --top 25[/dim]")


if __name__ == '__main__':
    main()
