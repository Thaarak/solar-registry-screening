#!/usr/bin/env python3
"""
Stage-0 deterministic pre-rank of all qualified ES targets.

Free, reproducible ranking that mirrors the 11 `screening` scorecard criteria
(48 pts) using pure rules over registry fields — no LLM, no DB writes. The
top-N ids feed the existing LLM screening:

    python scripts/run_scoring.py --dry-run --stage screening --ids "$(cat data/stage0_top100_ids.txt)"

Outputs:
    data/stage0_ranking.csv       full audit CSV (one row per target, per-criterion points)
    data/stage0_top100_ids.txt    single line, comma-separated top-N UUIDs

Usage:
    python scripts/run_stage0_ranking.py [options]

Options:
    --top N          Rows to show in the top table (default 25)
    --ids-out N      How many top ids to write to the ids file (default 100)
    --limit N        Score only first N targets (testing)
    --as-of DATE     Fixed scoring date YYYY-MM-DD (default: today)
    --verbose        Enable debug logging
"""

import argparse
import csv
import logging
import sys
from datetime import date
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.table import Table
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.scoring.stage0 import (
    CRITERIA,
    STAGE0_MAX_POINTS,
    classify_owner_name,
    compute_stage0,
)

console = Console()
logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / 'data'
RANKING_CSV_PATH = DATA_DIR / 'stage0_ranking.csv'
IDS_PATH = DATA_DIR / 'stage0_top100_ids.txt'

CRITERION_IDS = [cid for cid, _, _ in CRITERIA]


def load_targets(db, limit: int = None) -> list[dict]:
    """Qualified ES targets — identical filter to run_scoring so the top-N feeds through."""
    query = """
        SELECT id, plant_name, capacity_kw, commissioning_date, technology,
               municipality, province, region,
               tariff_regime, tariff_rate_eur_kwh, fit_remaining_years,
               tariff_expiry_date, poi_capacity_kw, grid_operator,
               curtailment_risk, grid_congestion_zone
        FROM targets
        WHERE country = 'ES'
          AND is_qualified = TRUE
        ORDER BY id
    """
    params = {}
    if limit is not None:
        query += " LIMIT :limit"
        params["limit"] = limit

    result = db.execute(text(query), params)
    rows = []
    for r in result:
        m = dict(r._mapping)
        m['id'] = str(m['id'])
        for k in ('capacity_kw', 'tariff_rate_eur_kwh', 'fit_remaining_years', 'poi_capacity_kw'):
            if m.get(k) is not None:
                m[k] = float(m[k])
        rows.append(m)
    return rows


def rank_targets(targets: list[dict], as_of: date) -> list[dict]:
    """Score, sort deterministically, assign 1-based rank."""
    scored = []
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Scoring...", total=len(targets))
        for t in targets:
            result = compute_stage0(t, as_of=as_of)
            scored.append({
                **t,
                'owner_name_class': classify_owner_name(t.get('plant_name')),
                'points': result.points,
                'total_points': result.total,
                'normalized_score': result.normalized,
            })
            progress.advance(task)

    # Sort: total desc, capacity desc, commissioning date asc (NULLs last), id.
    def sort_key(r):
        cd = r.get('commissioning_date')
        return (
            -r['total_points'],
            -(r['capacity_kw'] or 0),
            (cd is None, cd or date.max),
            r['id'],
        )

    scored.sort(key=sort_key)
    for i, r in enumerate(scored, start=1):
        r['rank'] = i
    return scored


def write_ranking_csv(ranked: list[dict]):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(RANKING_CSV_PATH, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([
            'target_id', 'rank', 'plant_name', 'owner_name_class',
            'municipality', 'province', 'region', 'capacity_kw',
            'commissioning_date', 'curtailment_risk',
            *CRITERION_IDS,
            'total_points', 'normalized_score',
        ])
        for r in ranked:
            writer.writerow([
                r['id'], r['rank'], r['plant_name'], r['owner_name_class'],
                r['municipality'], r['province'], r['region'], r['capacity_kw'],
                r['commissioning_date'], r['curtailment_risk'],
                *(r['points'][cid] for cid in CRITERION_IDS),
                r['total_points'], r['normalized_score'],
            ])


def write_ids_file(ranked: list[dict], count: int) -> int:
    top = ranked[:count]
    IDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    IDS_PATH.write_text(','.join(r['id'] for r in top), encoding='utf-8')
    return len(top)


def print_distribution(ranked: list[dict]):
    """Score distribution + per-criterion means."""
    n = len(ranked)

    totals = [r['total_points'] for r in ranked]
    dist = Table(title="Stage-0 Score Distribution")
    dist.add_column("Metric", style="cyan")
    dist.add_column("Value", style="green")
    dist.add_row("Targets scored", str(n))
    dist.add_row("Max points possible", str(STAGE0_MAX_POINTS))
    dist.add_row("Max observed", str(max(totals)))
    dist.add_row("Min observed", str(min(totals)))
    dist.add_row("Mean", f"{sum(totals) / n:.1f}")
    console.print(dist)
    console.print()

    means = Table(title="Per-Criterion Mean Points")
    means.add_column("Criterion", style="cyan")
    means.add_column("Max", style="dim")
    means.add_column("Mean", style="green")
    for cid, max_pts, _ in CRITERIA:
        mean = sum(r['points'][cid] for r in ranked) / n
        means.add_row(cid, str(max_pts), f"{mean:.2f}")
    console.print(means)
    console.print()


def print_top(ranked: list[dict], top: int):
    table = Table(title=f"Top {top} Targets (Stage-0 deterministic rank)")
    table.add_column("#", style="dim")
    table.add_column("Plant", style="cyan", max_width=32)
    table.add_column("Owner-name", style="dim")
    table.add_column("Province")
    table.add_column("kW", justify="right")
    table.add_column("Commissioned")
    table.add_column("Curtail")
    table.add_column("Pts", style="green", justify="right")
    for r in ranked[:top]:
        table.add_row(
            str(r['rank']),
            (r['plant_name'] or '')[:32],
            r['owner_name_class'],
            r['province'] or '',
            f"{r['capacity_kw']:.0f}" if r['capacity_kw'] is not None else '',
            str(r['commissioning_date'] or ''),
            r['curtailment_risk'] or '',
            f"{r['total_points']}/{STAGE0_MAX_POINTS}",
        )
    console.print(table)


def main():
    from src.db import get_db

    parser = argparse.ArgumentParser(
        description='Stage-0 deterministic pre-rank of qualified ES targets (no LLM, no DB writes)'
    )
    parser.add_argument('--top', type=int, default=25, help='Rows in the top table (default 25)')
    parser.add_argument('--ids-out', type=int, default=100, help='Top ids to write (default 100)')
    parser.add_argument('--limit', type=int, help='Score only first N targets (testing)')
    parser.add_argument('--as-of', type=date.fromisoformat, help='Fixed scoring date YYYY-MM-DD (default: today)')
    parser.add_argument('--verbose', '-v', action='store_true', help='Debug logging')

    args = parser.parse_args()
    if args.top < 1 or args.ids_out < 1 or (args.limit is not None and args.limit < 1):
        parser.error("--top, --ids-out and --limit must be positive")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    )

    as_of = args.as_of or date.today()

    console.print("[bold cyan]Stage-0 Deterministic Ranking (no LLM, no DB writes)[/bold cyan]")
    console.print(f"[dim]Scoring as of {as_of}[/dim]\n")

    console.print("[cyan]Step 1: Loading qualified ES targets...[/cyan]")
    with get_db() as db:
        targets = load_targets(db, limit=args.limit)
    console.print(f"[green]Loaded {len(targets)} qualified targets[/green]\n")

    if not targets:
        console.print("[yellow]Nothing to do![/yellow]")
        sys.exit(0)

    console.print("[cyan]Step 2: Scoring against 11 screening criteria...[/cyan]\n")
    ranked = rank_targets(targets, as_of)

    print_distribution(ranked)
    print_top(ranked, args.top)
    console.print()

    write_ranking_csv(ranked)
    console.print(f"[dim]Full ranking written to {RANKING_CSV_PATH}[/dim]")

    n_ids = write_ids_file(ranked, args.ids_out)
    console.print(f"[dim]Top-{n_ids} ids written to {IDS_PATH}[/dim]\n")

    console.print("[bold green]Stage-0 ranking complete![/bold green]")
    console.print("[dim]Next: preview the selected candidates for optional AI scoring:[/dim]")
    console.print(f'[dim]  python scripts/run_scoring.py --dry-run --stage screening --ids "$(cat {IDS_PATH.relative_to(Path.cwd()) if IDS_PATH.is_relative_to(Path.cwd()) else IDS_PATH})"[/dim]')


if __name__ == '__main__':
    main()
