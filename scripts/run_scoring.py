#!/usr/bin/env python3
"""
Run AI-powered scoring on solar plant targets.

Usage:
    python scripts/run_scoring.py [options]

Options:
    --top N         Score top N targets by capacity
    --all           Score all qualified targets
    --ids "id1,id2" Score specific target IDs
    --dry-run       Show targets but don't score
    --verbose       Enable debug logging
    --generate-memos  Generate memos for PURSUE targets
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from uuid import UUID

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.table import Table
from sqlalchemy import bindparam, text

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db import get_db
from src.scoring.schema import DEFAULT_SCHEMA_PATH, load_schema
from src.scoring.evaluation_agent import EvaluationAgent, TargetData
from src.scoring.memo_generator import MemoGenerator

console = Console()
logger = logging.getLogger(__name__)


def get_targets_for_scoring(db, target_ids: list[str] = None, top_n: int = None, all_targets: bool = False):
    """Get targets to score from database."""
    # Base query - join with cadastral_records to get referencia_catastral
    base_query = """
        SELECT
            t.id, t.plant_name, t.capacity_kw, t.commissioning_date,
            t.technology, t.latitude, t.longitude, t.municipality,
            t.province, t.region, t.tariff_regime, t.tariff_rate_eur_kwh,
            t.tariff_expiry_date, t.fit_remaining_years, t.grid_operator,
            t.poi_capacity_kw, t.curtailment_risk, t.grid_congestion_zone,
            t.is_operating, t.is_qualified,
            cr.referencia_catastral,
            o.owner_name, o.owner_type, o.approachability
        FROM targets t
        LEFT JOIN cadastral_records cr ON t.id = cr.target_id
        LEFT JOIN LATERAL (
            SELECT owner_name, owner_type, approachability
            FROM owners
            WHERE target_id = t.id
            ORDER BY created_at DESC, id
            LIMIT 1
        ) o ON TRUE
        WHERE t.country = 'ES'
          AND t.is_qualified = TRUE
    """

    if target_ids:
        query = text(base_query + " AND CAST(t.id AS text) IN :ids").bindparams(
            bindparam("ids", expanding=True)
        )
        result = db.execute(query, {"ids": target_ids})
    elif all_targets and top_n is None:
        result = db.execute(text(base_query + " ORDER BY t.capacity_kw DESC, t.id"))
    else:
        query = text(base_query + " ORDER BY t.capacity_kw DESC, t.id LIMIT :limit")
        result = db.execute(query, {"limit": top_n if top_n is not None else 10})

    targets = []
    for row in result:
        targets.append(TargetData.from_db_row(row))

    return targets


def check_already_scored(db, target_ids: list[str]) -> set[str]:
    """Check which targets have already been scored."""
    if not target_ids:
        return set()

    query = text("""
        SELECT DISTINCT CAST(target_id AS text) FROM scores
        WHERE CAST(target_id AS text) IN :ids
    """).bindparams(bindparam("ids", expanding=True))
    result = db.execute(query, {"ids": target_ids})

    return {str(row[0]) for row in result}


def insert_score(db, result):
    """Insert scoring result into database."""
    db_dict = result.to_db_dict()

    db.execute(
        text("""
            INSERT INTO scores (
                target_id, total_score, verdict,
                scoring_stage, screening_score, screening_verdict,
                revenue_contract_score, grid_repowering_score,
                resource_performance_score, technical_condition_score,
                land_permitting_score, commercial_structure_score,
                criteria_scores, gates_passed, failed_gates,
                overall_confidence, unknown_criteria,
                model_version, prompt_version, scored_at
            ) VALUES (
                :target_id, :total_score, :verdict,
                :scoring_stage, :screening_score, :screening_verdict,
                :revenue_contract_score, :grid_repowering_score,
                :resource_performance_score, :technical_condition_score,
                :land_permitting_score, :commercial_structure_score,
                CAST(:criteria_scores AS jsonb), :gates_passed, CAST(:failed_gates AS jsonb),
                :overall_confidence, CAST(:unknown_criteria AS jsonb),
                :model_version, :prompt_version, :scored_at
            )
        """),
        db_dict
    )


def main():
    parser = argparse.ArgumentParser(
        description='Run AI-powered scoring on solar plant targets'
    )
    parser.add_argument('--top', type=int, help='Score top N targets by capacity')
    parser.add_argument('--all', action='store_true', help='Score all qualified targets')
    parser.add_argument('--ids', type=str, help='Comma-separated target IDs to score')
    parser.add_argument('--dry-run', action='store_true', help='Show targets without scoring')
    parser.add_argument('--verbose', '-v', action='store_true', help='Debug logging')
    parser.add_argument('--generate-memos', action='store_true', help='Generate memos for PURSUE targets')
    parser.add_argument('--skip-scored', action='store_true', help='Skip targets already scored')
    parser.add_argument('--delay', type=float, default=0.5, help='Delay between API calls (seconds)')
    parser.add_argument(
        '--stage', choices=['screening', 'dd', 'full'], default='screening',
        help='Scoring stage: screening (Stage-1 triage on registry-derivable criteria, '
             'normalized 0-100 ranking) or dd/full (Stage-2 absolute verdict on all criteria)'
    )

    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA_PATH, help="Scorecard JSON")
    parser.add_argument("--model", default=os.getenv("ANTHROPIC_MODEL") or None)
    parser.add_argument("--allow-external-scoring", action="store_true",
                        help="Allow selected technical fields to be sent to Anthropic")
    args = parser.parse_args()
    if not args.dry_run and not args.allow_external_scoring:
        parser.error("live scoring requires --allow-external-scoring")
    if args.top is not None and args.top < 1:
        parser.error("--top must be positive")
    if args.ids:
        try:
            args.ids = [str(UUID(value.strip())) for value in args.ids.split(",")]
        except ValueError:
            parser.error("--ids must contain comma-separated UUIDs")

    # Setup logging
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    console.print("[bold cyan]Solar Plant Scoring Engine[/bold cyan]\n")

    # Load schema
    schema_path = args.schema
    console.print(f"[cyan]Loading schema from {schema_path}...[/cyan]")

    try:
        schema = load_schema(schema_path)
        if errors := schema.validate():
            raise ValueError("; ".join(errors))
        console.print(f"[green]Loaded schema: {len(schema.criteria)} criteria, {len(schema.deal_breakers)} deal-breakers[/green]\n")
    except Exception as e:
        console.print(f"[red]Failed to load schema: {e}[/red]")
        sys.exit(1)

    # Get targets
    console.print("[cyan]Loading targets from database...[/cyan]")

    with get_db() as db:
        targets = get_targets_for_scoring(
            db,
            target_ids=args.ids,
            top_n=args.top,
            all_targets=args.all,
        )

    console.print(f"[green]Found {len(targets)} targets to score[/green]\n")

    if not targets:
        console.print("[yellow]No targets found matching criteria![/yellow]")
        sys.exit(0)

    # Check for already scored (optional)
    if args.skip_scored:
        with get_db() as db:
            already_scored = check_already_scored(db, [t.id for t in targets])
        if already_scored:
            console.print(f"[yellow]Skipping {len(already_scored)} already-scored targets[/yellow]")
            targets = [t for t in targets if t.id not in already_scored]

    # Show sample
    console.print("[cyan]Targets to score:[/cyan]")
    sample_table = Table()
    sample_table.add_column("Name", style="cyan")
    sample_table.add_column("Capacity", style="green")
    sample_table.add_column("Province", style="yellow")
    sample_table.add_column("Commissioned", style="dim")

    for t in targets[:10]:
        sample_table.add_row(
            t.plant_name or "Unknown",
            f"{t.capacity_kw:.0f} kW" if t.capacity_kw else "N/A",
            t.province or "Unknown",
            str(t.commissioning_date) if t.commissioning_date else "N/A",
        )

    if len(targets) > 10:
        sample_table.add_row("...", f"and {len(targets) - 10} more", "", "")

    console.print(sample_table)
    console.print()

    if args.dry_run:
        console.print("[yellow]Dry run - not scoring targets[/yellow]")
        sys.exit(0)

    # Check API key
    if not os.environ.get('ANTHROPIC_API_KEY'):
        console.print("[red]ANTHROPIC_API_KEY not set. Set it to score targets.[/red]")
        sys.exit(1)

    # Initialize agent
    console.print("[cyan]Initializing evaluation agent...[/cyan]")
    agent_options = {"model": args.model} if args.model else {}
    agent = EvaluationAgent(schema, delay_seconds=args.delay, stage=args.stage, **agent_options)

    n_scored = len(agent.scored_criteria)
    if args.stage == 'screening':
        console.print(
            f"[cyan]Stage: SCREENING[/cyan] — Stage-1 triage on {n_scored} "
            f"registry-derivable criteria ({schema.screening_max_points()} pts), "
            f"ranked by normalized screening_score (0-100)."
        )
    else:
        console.print(
            f"[cyan]Stage: {args.stage.upper()}[/cyan] — Stage-2 absolute verdict "
            f"on all {n_scored} criteria (0-100)."
        )

    # Score targets
    console.print(f"\n[cyan]Scoring {len(targets)} targets...[/cyan]")
    console.print(f"[dim]Rate limited to {args.delay}s between requests[/dim]\n")

    results = []
    errors = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Scoring...", total=len(targets))

        for target in targets:
            try:
                result = agent.score_target(target)
                results.append((target, result))

                # Log result
                logger.debug(
                    f"Scored {target.plant_name}: {result.total_score:.1f} ({result.verdict})"
                )

            except Exception as e:
                logger.error(f"Failed to score {target.id}: {e}")
                errors.append((target, str(e)))

            progress.advance(task)

            # Rate limiting
            time.sleep(args.delay)

    # Store results
    console.print(f"\n[cyan]Storing {len(results)} scores in database...[/cyan]")

    with get_db() as db:
        for target, result in results:
            try:
                insert_score(db, result)
            except Exception as e:
                logger.error(f"Failed to store score for {target.id}: {e}")
                errors.append((target, f"DB error: {e}"))

    # Results summary
    console.print()

    results_table = Table(title="Scoring Results")
    results_table.add_column("Metric", style="cyan")
    results_table.add_column("Value", style="green")

    pursue = sum(1 for _, r in results if r.verdict == 'PURSUE')
    conditional = sum(1 for _, r in results if r.verdict == 'CONDITIONAL')
    pass_verdict = sum(1 for _, r in results if r.verdict == 'PASS')

    results_table.add_row("Targets scored", str(len(results)))
    results_table.add_row("PURSUE", f"🟢 {pursue}")
    results_table.add_row("CONDITIONAL", f"🟡 {conditional}")
    results_table.add_row("PASS", f"🔴 {pass_verdict}")
    results_table.add_row("Errors", str(len(errors)))

    console.print(results_table)

    # Show ranked targets
    if args.stage == 'screening':
        # Stage 1: rank ALL passing targets by normalized screening_score (triage).
        ranked = sorted(
            [(t, r) for t, r in results if r.gates_passed],
            key=lambda x: x[1].screening_score,
            reverse=True
        )
        if ranked:
            console.print("\n[bold green]Top Ranked Targets (by screening_score):[/bold green]")
            rank_table = Table()
            rank_table.add_column("Name", style="cyan")
            rank_table.add_column("Screening", style="green")
            rank_table.add_column("Verdict", style="magenta")
            rank_table.add_column("Capacity", style="yellow")
            rank_table.add_column("Province", style="dim")

            for target, result in ranked[:20]:
                rank_table.add_row(
                    target.plant_name or "Unknown",
                    f"{result.screening_score:.1f}/100",
                    result.screening_verdict,
                    f"{target.capacity_kw:.0f} kW" if target.capacity_kw else "N/A",
                    target.province or "Unknown",
                )

            console.print(rank_table)
    elif pursue > 0:
        # Stage 2: absolute PURSUE list ranked by total_score.
        console.print("\n[bold green]Top PURSUE Targets:[/bold green]")

        pursue_table = Table()
        pursue_table.add_column("Name", style="cyan")
        pursue_table.add_column("Score", style="green")
        pursue_table.add_column("Capacity", style="yellow")
        pursue_table.add_column("Province", style="dim")

        pursue_targets = sorted(
            [(t, r) for t, r in results if r.verdict == 'PURSUE'],
            key=lambda x: x[1].total_score,
            reverse=True
        )

        for target, result in pursue_targets[:10]:
            pursue_table.add_row(
                target.plant_name or "Unknown",
                f"{result.total_score:.1f}/100",
                f"{target.capacity_kw:.0f} kW" if target.capacity_kw else "N/A",
                target.province or "Unknown",
            )

        console.print(pursue_table)

    # Generate memos if requested
    if args.generate_memos and pursue > 0:
        console.print("\n[cyan]Generating memos for PURSUE targets...[/cyan]")

        memo_dir = Path(__file__).parent.parent / 'data' / 'memos'
        memo_dir.mkdir(exist_ok=True)

        generator = MemoGenerator(schema)
        memo_count = 0

        memo_targets = sorted(
            [(t, r) for t, r in results if r.verdict == 'PURSUE'],
            key=lambda x: x[1].total_score,
            reverse=True
        )

        for target, result in memo_targets[:10]:
            try:
                memo_path = memo_dir / f"memo_{target.id[:8]}.md"
                generator.generate_memo(target, result, memo_path)
                memo_count += 1
            except Exception as e:
                logger.error(f"Failed to generate memo for {target.id}: {e}")

        console.print(f"[green]Generated {memo_count} memos in {memo_dir}[/green]")

    # Show errors if any
    if errors:
        console.print("\n[red]Errors:[/red]")
        for target, error in errors[:5]:
            console.print(f"  - {target.plant_name or target.id}: {error}")

    # API stats
    stats = agent.get_stats()
    console.print(f"\n[dim]API stats: {stats}[/dim]")

    console.print("\n[bold green]Scoring complete![/bold green]")


if __name__ == '__main__':
    main()
