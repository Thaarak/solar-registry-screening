#!/usr/bin/env python3
"""
Enrich targets with coordinates from regional installation registries.

Uses the PRETOR `regional_registry_id` (Número de Registro Autonómico
Definitivo) as a deterministic join key into per-region installation registries,
falling back to municipality-scoped capacity/area matching. Sources are
pluggable: bulk static files (reachable, e.g. JCyL shapefile) and the regional
WFS hosts (geofenced — run from Spanish egress).

Usage:
    python scripts/enrich_regional.py [options]

Options:
    --source {file,wfs}       Coordinate source (default: file)
    --regions "A,B"           Comma-separated regions (default: the 5 gap regions)
    --dry-run                 Show matches without updating the database
    --limit N                 Process only first N targets per region (testing)
    --threshold N             Confidence threshold (default: 0.70)
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
from src.enrichment.regional_registry import (
    DEFAULT_REGIONS,
    PROVINCE_TO_REGION,
    RegistryMatcher,
    make_source,
)
from src.models import SPAIN_BOUNDS

console = Console()
logger = logging.getLogger(__name__)

REPORT_PATH = Path(__file__).parent.parent / 'data' / 'regional_matches.csv'


def load_targets_by_region(db, regions: list[str]) -> dict[str, list[dict]]:
    """Missing-coordinate ES targets in the given regions, grouped by region."""
    result = db.execute(text("""
        SELECT id, plant_name, capacity_kw, regional_registry_id, municipality, province
        FROM targets
        WHERE country = 'ES'
          AND (latitude IS NULL OR longitude IS NULL)
          AND province IS NOT NULL
        ORDER BY province, municipality
    """))

    groups: dict[str, list[dict]] = {r: [] for r in regions}
    for row in result:
        region = PROVINCE_TO_REGION.get((row.province or '').strip().upper())
        if region not in groups:
            continue
        groups[region].append({
            'id': row.id,
            'plant_name': row.plant_name,
            'capacity_kw': float(row.capacity_kw) if row.capacity_kw is not None else None,
            'regional_registry_id': row.regional_registry_id,
            'municipality': row.municipality,
            'province': row.province,
        })
    return groups


def in_spain_bounds(lat: float, lon: float) -> bool:
    return (SPAIN_BOUNDS['min_lat'] <= lat <= SPAIN_BOUNDS['max_lat']
            and SPAIN_BOUNDS['min_lon'] <= lon <= SPAIN_BOUNDS['max_lon'])


def update_target_coordinates(db, match):
    """Write matched coordinates + provenance to the targets table."""
    db.execute(
        text("""
            UPDATE targets
            SET latitude = :lat,
                longitude = :lon,
                qualification_notes = COALESCE(qualification_notes, '') ||
                    ' [Coordinates from regional registry: ' || :note || ']',
                updated_at = NOW()
            WHERE id = :target_id
        """),
        {
            'lat': match.osm_plant.latitude,
            'lon': match.osm_plant.longitude,
            'note': f"{match.osm_plant.osm_id}, {match.method}, conf {match.confidence:.2f}",
            'target_id': match.target_id,
        },
    )


def write_report(matches):
    """Write audit CSV of all matches."""
    with open(REPORT_PATH, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([
            'target_id', 'plant_name', 'municipality', 'province',
            'target_capacity_kw', 'source_id', 'source_name', 'source_capacity_kw',
            'regional_ref', 'latitude', 'longitude', 'method', 'confidence',
        ])
        for m in matches:
            writer.writerow([
                m.target_id, m.target_name, m.municipality, m.province,
                m.target_capacity_kw, m.osm_plant.osm_id, m.osm_plant.name,
                m.osm_plant.capacity_kw,
                m.details.get('regional_ref', ''),
                f"{m.osm_plant.latitude:.6f}", f"{m.osm_plant.longitude:.6f}",
                m.method, f"{m.confidence:.2f}",
            ])


def main():
    parser = argparse.ArgumentParser(
        description='Enrich targets with coordinates from regional registries'
    )
    parser.add_argument('--source', choices=['file', 'wfs'], default='file',
                        help='Coordinate source: file (bulk download, e.g. JCyL), '
                             'wfs (regional GIS, geofenced). Default: file')
    parser.add_argument('--regions', type=str,
                        help='Comma-separated regions (default: the 5 gap regions)')
    parser.add_argument('--dry-run', action='store_true', help='Show matches without updating')
    parser.add_argument('--limit', type=int, help='Process only first N targets per region')
    parser.add_argument('--threshold', type=float, default=0.70, help='Confidence threshold')
    parser.add_argument('--verbose', '-v', action='store_true', help='Debug logging')

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    )
    if not args.verbose:
        logging.getLogger('urllib3').setLevel(logging.WARNING)

    regions = (
        [r.strip().upper() for r in args.regions.split(',')]
        if args.regions else list(DEFAULT_REGIONS)
    )

    console.print("[bold cyan]Regional Registry Coordinate Enrichment[/bold cyan]\n")
    console.print(f"[dim]Source: {args.source} | Regions: {', '.join(regions)}[/dim]\n")

    # Step 1: targets needing coordinates, grouped by region
    console.print("[cyan]Step 1: Loading targets needing coordinates...[/cyan]")
    with get_db() as db:
        groups = load_targets_by_region(db, regions)

    total_targets = sum(len(v) for v in groups.values())
    for r in regions:
        console.print(f"  {r}: {len(groups.get(r, []))} targets")
    console.print(f"[green]Total: {total_targets} targets across {len(regions)} regions[/green]\n")

    if not total_targets:
        console.print("[yellow]Nothing to do![/yellow]")
        sys.exit(0)

    # Step 2: fetch + match per region
    console.print(f"[cyan]Step 2: Fetching from '{args.source}' source and matching...[/cyan]\n")
    source = make_source(args.source)
    matcher = RegistryMatcher(confidence_threshold=args.threshold)

    all_matches = []
    fetch_counts = {}

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Regions...", total=len(regions))
        for region in regions:
            targets = groups.get(region, [])
            if args.limit:
                targets = targets[:args.limit]
            progress.update(task, description=f"{region} ({len(targets)} targets)")

            if targets:
                installations = source.fetch(region)
                fetch_counts[region] = len(installations)
                matches = matcher.match_province(targets, installations, region)
                all_matches.extend(matches)
            else:
                fetch_counts[region] = 0

            progress.advance(task)

    # Step 3: results
    m_stats = matcher.get_stats()
    stats_table = Table(title="Matching Results")
    stats_table.add_column("Metric", style="cyan")
    stats_table.add_column("Value", style="green")
    stats_table.add_row("Installations fetched", str(sum(fetch_counts.values())))
    stats_table.add_row("Matched (registry-id join)", str(m_stats['matched_ref']))
    stats_table.add_row("Matched (capacity/area)", str(m_stats['matched_capacity']))
    stats_table.add_row("Rejected (capacity mismatch)", str(m_stats['rejected_capacity']))
    stats_table.add_row("Skipped (ambiguous)", str(m_stats['ambiguous_skipped']))
    stats_table.add_row("Unmatched", str(m_stats['unmatched']))
    console.print(stats_table)
    console.print()

    if hasattr(source, 'get_stats'):
        console.print(f"[dim]Source stats: {source.get_stats()}[/dim]\n")

    if not all_matches:
        console.print("[yellow]No matches found.[/yellow]")
        if args.source == 'wfs':
            console.print(
                "[dim]The regional WFS hosts are geofenced; run --source wfs from "
                "Spanish egress (see scripts/probe_regional_wfs.py) to scale.[/dim]"
            )
        sys.exit(0)

    # Validate coordinates fall inside Spain
    valid_matches = []
    for m in all_matches:
        if in_spain_bounds(m.osm_plant.latitude, m.osm_plant.longitude):
            valid_matches.append(m)
        else:
            logger.warning(f"Match for {m.target_id} outside Spain bounds, dropping")

    console.print("[cyan]Sample matches:[/cyan]")
    for m in valid_matches[:10]:
        cap = f"{m.target_capacity_kw:.0f} kW" if m.target_capacity_kw else "?"
        console.print(
            f"  [{m.confidence:.0%}] {m.target_name or '?'} ({cap}, {m.municipality}) "
            f"→ {m.osm_plant.osm_id} [{m.method}]"
        )
    if len(valid_matches) > 10:
        console.print(f"  ... and {len(valid_matches) - 10} more")
    console.print()

    write_report(valid_matches)
    console.print(f"[dim]Audit report written to {REPORT_PATH}[/dim]\n")

    if args.dry_run:
        console.print("[yellow]Dry run - not updating database[/yellow]")
        console.print(f"[dim]Would update {len(valid_matches)} targets with coordinates[/dim]")
        sys.exit(0)

    # Step 4: update database
    console.print("[cyan]Step 3: Updating database...[/cyan]")
    updated = 0
    with get_db() as db:
        for m in valid_matches:
            try:
                update_target_coordinates(db, m)
                updated += 1
            except Exception as e:
                logger.error(f"Failed to update {m.target_id}: {e}")

    console.print(f"[green]Updated {updated} targets with coordinates![/green]\n")

    # Final coverage summary
    with engine.connect() as conn:
        total = conn.execute(text(
            "SELECT COUNT(*) FROM targets WHERE country = 'ES'"
        )).scalar()
        with_coords = conn.execute(text(
            "SELECT COUNT(*) FROM targets WHERE country = 'ES' AND latitude IS NOT NULL"
        )).scalar()

    summary = Table(title="Coordinate Coverage")
    summary.add_column("Metric", style="cyan")
    summary.add_column("Value", style="green")
    summary.add_row("Total Spanish targets", str(total))
    summary.add_row("With coordinates", str(with_coords))
    summary.add_row("Coverage", f"{100 * with_coords / total:.1f}%")
    console.print(summary)

    console.print("\n[bold green]Regional enrichment complete![/bold green]")
    console.print("[dim]Run cadastral lookups next: python scripts/run_cadastral_lookups.py[/dim]")


if __name__ == '__main__':
    main()
