#!/usr/bin/env python3
"""
Enrich targets with coordinates via municipality-scoped OSM spatial matching.

Queries Overpass for power=plant + plant:source=solar polygons inside each
target municipality (only municipalities with few targets), assigns polygons
to targets using capacity disambiguation, and updates the database.

Usage:
    python scripts/enrich_osm_spatial.py [options]

Options:
    --dry-run           Show matches without updating database
    --limit N           Process only first N municipalities (for testing)
    --max-per-muni N    Max targets per municipality to attempt (default: 3)
    --threshold N       Confidence threshold (default: 0.70)
    --delay N           Delay between Overpass queries in seconds (default: 1.5)
    --no-cache          Ignore cached Overpass responses
    --verbose           Enable debug logging
"""

import argparse
import csv
import json
import logging
import re
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.table import Table
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db import get_db, engine
from src.enrichment.osm_spatial import (
    MunicipalityOverpassClient,
    SpatialMatcher,
    parse_overpass_plants,
)
from src.models import SPAIN_BOUNDS

console = Console()
logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).parent.parent / 'data' / 'cache' / 'osm_spatial'
REPORT_PATH = Path(__file__).parent.parent / 'data' / 'osm_spatial_matches.csv'


def slugify(value: str) -> str:
    return re.sub(r'[^a-z0-9]+', '_', value.lower()).strip('_')


def get_municipality_groups(db, max_per_muni: int) -> list[dict]:
    """Municipalities (with <= max_per_muni targets) whose targets lack coordinates."""
    result = db.execute(text("""
        WITH muni_counts AS (
            SELECT municipality, province, COUNT(*) AS n
            FROM targets
            WHERE country = 'ES' AND municipality IS NOT NULL
            GROUP BY municipality, province
        )
        SELECT t.id, t.plant_name, t.capacity_kw, t.municipality, t.province, mc.n
        FROM targets t
        JOIN muni_counts mc
          ON t.municipality = mc.municipality AND t.province = mc.province
        WHERE t.country = 'ES'
          AND (t.latitude IS NULL OR t.longitude IS NULL)
          AND mc.n <= :max_n
        ORDER BY mc.n ASC, t.province, t.municipality
    """), {'max_n': max_per_muni})

    groups: dict[tuple, dict] = {}
    for row in result:
        key = (row.municipality, row.province)
        if key not in groups:
            groups[key] = {
                'municipality': row.municipality,
                'province': row.province,
                'total_in_muni': row.n,
                'targets': [],
            }
        groups[key]['targets'].append({
            'id': row.id,
            'plant_name': row.plant_name,
            'capacity_kw': row.capacity_kw,
        })
    return list(groups.values())


def fetch_plants(client, municipality: str, province: str, use_cache: bool):
    """Fetch Overpass plants for a municipality, with on-disk JSON cache."""
    cache_file = CACHE_DIR / f"{slugify(province)}__{slugify(municipality)}.json"

    if use_cache and cache_file.exists():
        try:
            data = json.loads(cache_file.read_text())
            return parse_overpass_plants(data), True
        except (json.JSONDecodeError, OSError):
            pass  # fall through to live query

    data = client.query_municipality(municipality, province)
    if data is None:
        return None, False

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(data))
    return parse_overpass_plants(data), False


def update_target_coordinates(db, match):
    """Write matched coordinates to the targets table."""
    db.execute(
        text("""
            UPDATE targets
            SET latitude = :lat,
                longitude = :lon,
                qualification_notes = COALESCE(qualification_notes, '') ||
                    ' [Coordinates from OSM spatial match: ' || :note || ']',
                updated_at = NOW()
            WHERE id = :target_id
        """),
        {
            'lat': match.osm_plant.latitude,
            'lon': match.osm_plant.longitude,
            'note': f"{match.osm_plant.osm_id}, {match.method}, conf {match.confidence:.2f}",
            'target_id': match.target_id,
        }
    )


def in_spain_bounds(lat: float, lon: float) -> bool:
    return (SPAIN_BOUNDS['min_lat'] <= lat <= SPAIN_BOUNDS['max_lat']
            and SPAIN_BOUNDS['min_lon'] <= lon <= SPAIN_BOUNDS['max_lon'])


def write_report(matches):
    """Write audit CSV of all matches."""
    with open(REPORT_PATH, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([
            'target_id', 'plant_name', 'municipality', 'province',
            'target_capacity_kw', 'osm_id', 'osm_name', 'osm_capacity_kw',
            'osm_area_m2', 'latitude', 'longitude', 'method', 'confidence',
        ])
        for m in matches:
            writer.writerow([
                m.target_id, m.target_name, m.municipality, m.province,
                m.target_capacity_kw, m.osm_plant.osm_id, m.osm_plant.name,
                m.osm_plant.capacity_kw,
                f"{m.osm_plant.area_m2:.0f}" if m.osm_plant.area_m2 else '',
                f"{m.osm_plant.latitude:.6f}", f"{m.osm_plant.longitude:.6f}",
                m.method, f"{m.confidence:.2f}",
            ])


def main():
    parser = argparse.ArgumentParser(
        description='Enrich targets with coordinates via OSM spatial matching'
    )
    parser.add_argument('--dry-run', action='store_true', help='Show matches without updating')
    parser.add_argument('--limit', type=int, help='Process only first N municipalities')
    parser.add_argument('--max-per-muni', type=int, default=3,
                        help='Max targets per municipality to attempt (default: 3)')
    parser.add_argument('--threshold', type=float, default=0.70, help='Confidence threshold')
    parser.add_argument('--delay', type=float, default=1.5, help='Delay between queries (s)')
    parser.add_argument('--no-cache', action='store_true', help='Ignore cached responses')
    parser.add_argument('--verbose', '-v', action='store_true', help='Debug logging')

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    # Quiet noisy libs unless verbose
    if not args.verbose:
        logging.getLogger('urllib3').setLevel(logging.WARNING)

    console.print("[bold cyan]OSM Spatial Coordinate Enrichment[/bold cyan]\n")

    # Step 1: Load municipality groups
    console.print("[cyan]Step 1: Loading municipalities needing coordinates...[/cyan]")
    with get_db() as db:
        groups = get_municipality_groups(db, args.max_per_muni)

    total_targets = sum(len(g['targets']) for g in groups)
    console.print(
        f"[green]Found {len(groups)} municipalities "
        f"(≤{args.max_per_muni} plants each) covering {total_targets} targets[/green]\n"
    )

    if args.limit:
        groups = groups[:args.limit]
        console.print(f"[yellow]Limited to first {len(groups)} municipalities[/yellow]\n")

    if not groups:
        console.print("[yellow]Nothing to do![/yellow]")
        sys.exit(0)

    # Step 2: Query + match
    console.print("[cyan]Step 2: Querying Overpass and matching...[/cyan]")
    console.print(f"[dim]Rate limited to {args.delay}s between live queries[/dim]\n")

    client = MunicipalityOverpassClient(delay_seconds=args.delay)
    matcher = SpatialMatcher(confidence_threshold=args.threshold)

    all_matches = []
    failed_munis = []
    empty_munis = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Querying...", total=len(groups))

        for g in groups:
            muni, prov = g['municipality'], g['province']
            progress.update(task, description=f"{muni[:30]} ({prov})")

            plants, from_cache = fetch_plants(client, muni, prov, use_cache=not args.no_cache)

            if plants is None:
                failed_munis.append((muni, prov))
            else:
                if not plants:
                    empty_munis += 1
                matches = matcher.match_municipality(g['targets'], plants, muni, prov)
                all_matches.extend(matches)

            progress.advance(task)
            if not from_cache and plants is not None:
                time.sleep(args.delay)

    # Step 3: Results
    m_stats = matcher.get_stats()
    c_stats = client.get_stats()

    stats_table = Table(title="Matching Results")
    stats_table.add_column("Metric", style="cyan")
    stats_table.add_column("Value", style="green")
    stats_table.add_row("Municipalities processed", str(len(groups)))
    stats_table.add_row("Overpass query failures", str(len(failed_munis)))
    stats_table.add_row("Municipalities with no OSM polygons", str(empty_munis))
    stats_table.add_row("Targets matched", str(m_stats['matched']))
    stats_table.add_row("Rejected (capacity mismatch)", str(m_stats['rejected_capacity']))
    stats_table.add_row("Skipped (ambiguous)", str(m_stats['ambiguous_skipped']))
    console.print(stats_table)
    console.print()

    if failed_munis:
        console.print(f"[yellow]Failed municipalities ({len(failed_munis)}):[/yellow]")
        for muni, prov in failed_munis[:10]:
            console.print(f"  - {muni} ({prov})")
        if len(failed_munis) > 10:
            console.print(f"  ... and {len(failed_munis) - 10} more")
        console.print()

    if not all_matches:
        console.print("[yellow]No matches found![/yellow]")
        sys.exit(0)

    # Validate coordinates
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

    # Audit report
    write_report(valid_matches)
    console.print(f"[dim]Audit report written to {REPORT_PATH}[/dim]\n")

    if args.dry_run:
        console.print("[yellow]Dry run - not updating database[/yellow]")
        console.print(f"[dim]Would update {len(valid_matches)} targets with coordinates[/dim]")
        sys.exit(0)

    # Step 4: Update database
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

    # Final summary
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

    console.print("\n[bold green]Spatial enrichment complete![/bold green]")
    console.print("[dim]Run cadastral lookups next: python scripts/run_cadastral_lookups.py[/dim]")


if __name__ == '__main__':
    main()
