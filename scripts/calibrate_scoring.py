#!/usr/bin/env python3
"""
Calibration workflow for scoring agent.

Usage:
    # Export targets for hand-scoring
    python scripts/calibrate_scoring.py export --count 10

    # Import hand scores and compare
    python scripts/calibrate_scoring.py compare --human data/human_scores.csv

    # Generate calibration report
    python scripts/calibrate_scoring.py report
"""

import argparse
import csv
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table
from sqlalchemy import bindparam, text

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db import get_db, engine
from src.scoring.schema import DEFAULT_SCHEMA_PATH, load_schema

console = Console()
logger = logging.getLogger(__name__)


def export_for_calibration(db, count: int, output_path: Path, schema_path: Path):
    """Export targets for human scoring."""
    # Get diverse sample of targets
    query = """
        SELECT
            t.id, t.plant_name, t.capacity_kw, t.commissioning_date,
            t.technology, t.latitude, t.longitude, t.municipality,
            t.province, t.tariff_regime, t.is_operating,
            cr.referencia_catastral
        FROM targets t
        LEFT JOIN cadastral_records cr ON t.id = cr.target_id
        WHERE t.country = 'ES'
          AND t.is_qualified = TRUE
        ORDER BY RANDOM()
        LIMIT :count
    """

    result = db.execute(text(query), {'count': count})
    rows = list(result)

    # Load schema for criteria headers
    schema = load_schema(schema_path)

    # Write CSV
    fieldnames = [
        'target_id', 'plant_name', 'capacity_kw', 'commissioning_date',
        'province', 'tariff_regime', 'has_coordinates', 'has_cadastral'
    ]

    # Add criteria columns
    for criterion in schema.criteria:
        fieldnames.append(f"score_{criterion.id}")
        fieldnames.append(f"confidence_{criterion.id}")
        fieldnames.append(f"notes_{criterion.id}")

    fieldnames.extend(['total_score', 'verdict', 'notes'])

    with open(output_path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for row in rows:
            data = {
                'target_id': str(row.id),
                'plant_name': row.plant_name,
                'capacity_kw': float(row.capacity_kw) if row.capacity_kw else '',
                'commissioning_date': str(row.commissioning_date) if row.commissioning_date else '',
                'province': row.province,
                'tariff_regime': row.tariff_regime,
                'has_coordinates': 'Yes' if row.latitude else 'No',
                'has_cadastral': 'Yes' if row.referencia_catastral else 'No',
            }

            # Initialize empty score columns
            for criterion in schema.criteria:
                data[f"score_{criterion.id}"] = ''
                data[f"confidence_{criterion.id}"] = ''
                data[f"notes_{criterion.id}"] = ''

            data['total_score'] = ''
            data['verdict'] = ''
            data['notes'] = ''

            writer.writerow(data)

    console.print(f"[green]Exported {len(rows)} targets to {output_path}[/green]")

    # Also create a scoring guide
    guide_path = output_path.parent / 'scoring_guide.md'
    with open(guide_path, 'w') as f:
        f.write("# Scoring Guide\n\n")
        f.write("Use this guide to hand-score the calibration targets.\n\n")

        for category in schema.categories:
            f.write(f"## {category.name} ({category.max_points} points)\n\n")

            for criterion in category.criteria:
                f.write(f"### {criterion.id}: {criterion.name}\n")
                f.write(f"{criterion.description}\n\n")
                f.write("Anchors:\n")
                for score in sorted(criterion.anchors.keys(), reverse=True):
                    f.write(f"- **{score} points**: {criterion.anchors[score]}\n")
                f.write("\n")

        f.write("## Verdict Thresholds\n\n")
        f.write(f"- PURSUE: >= {schema.verdict_thresholds['pursue']} points\n")
        f.write(f"- CONDITIONAL: >= {schema.verdict_thresholds['conditional']} points\n")
        f.write(f"- PASS: < {schema.verdict_thresholds['conditional']} points\n")

    console.print(f"[green]Created scoring guide at {guide_path}[/green]")


def compare_scores(db, human_scores_path: Path, schema_path: Path):
    """Compare human scores with agent scores."""
    schema = load_schema(schema_path)

    # Load human scores
    human_scores = {}
    with open(human_scores_path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get('total_score'):
                human_scores[row['target_id']] = {
                    'total_score': float(row['total_score']),
                    'verdict': row.get('verdict', ''),
                    'criteria': {},
                }
                for criterion in schema.criteria:
                    score_key = f"score_{criterion.id}"
                    if row.get(score_key):
                        human_scores[row['target_id']]['criteria'][criterion.id] = float(row[score_key])

    if not human_scores:
        console.print("[yellow]No human scores found in file![/yellow]")
        return

    console.print(f"[green]Loaded {len(human_scores)} human scores[/green]")

    # Load agent scores
    target_ids = list(human_scores.keys())
    query = text("""
        SELECT CAST(target_id AS text) AS target_id, total_score, verdict, criteria_scores
        FROM scores
        WHERE CAST(target_id AS text) IN :ids
        ORDER BY scored_at DESC
    """).bindparams(bindparam("ids", expanding=True))
    result = db.execute(query, {"ids": target_ids})

    agent_scores = {}
    for row in result:
        if row.target_id not in agent_scores:  # Keep most recent
            agent_scores[row.target_id] = {
                'total_score': float(row.total_score),
                'verdict': row.verdict,
                'criteria': {},
            }
            criteria_data = json.loads(row.criteria_scores) if isinstance(row.criteria_scores, str) else row.criteria_scores
            for cs in criteria_data:
                agent_scores[row.target_id]['criteria'][cs['criterion_id']] = cs['score']

    console.print(f"[green]Loaded {len(agent_scores)} agent scores[/green]")

    # Compare
    comparison = []
    for target_id in human_scores:
        if target_id not in agent_scores:
            console.print(f"[yellow]Target {target_id} not scored by agent[/yellow]")
            continue

        human = human_scores[target_id]
        agent = agent_scores[target_id]

        diff = agent['total_score'] - human['total_score']

        comparison.append({
            'target_id': target_id,
            'human_score': human['total_score'],
            'agent_score': agent['total_score'],
            'difference': diff,
            'human_verdict': human['verdict'],
            'agent_verdict': agent['verdict'],
            'verdict_match': human['verdict'].upper() == agent['verdict'].upper(),
        })

    if not comparison:
        console.print("[red]No matching scores found![/red]")
        return

    # Summary statistics
    diffs = [c['difference'] for c in comparison]
    avg_diff = sum(diffs) / len(diffs)
    abs_diffs = [abs(d) for d in diffs]
    avg_abs_diff = sum(abs_diffs) / len(abs_diffs)
    max_diff = max(abs_diffs)
    verdict_matches = sum(1 for c in comparison if c['verdict_match'])

    console.print("\n[bold]Calibration Results[/bold]\n")

    summary_table = Table(title="Summary Statistics")
    summary_table.add_column("Metric", style="cyan")
    summary_table.add_column("Value", style="green")

    summary_table.add_row("Targets compared", str(len(comparison)))
    summary_table.add_row("Average difference", f"{avg_diff:+.1f}")
    summary_table.add_row("Average absolute difference", f"{avg_abs_diff:.1f}")
    summary_table.add_row("Max absolute difference", f"{max_diff:.1f}")
    summary_table.add_row("Verdict agreement", f"{verdict_matches}/{len(comparison)} ({100*verdict_matches/len(comparison):.0f}%)")

    # Target: <10 points divergence
    target_met = avg_abs_diff < 10
    summary_table.add_row(
        "Target (<10 pts avg divergence)",
        f"{'PASS' if target_met else 'FAIL'}"
    )

    console.print(summary_table)

    # Detail table
    console.print("\n[bold]Detailed Comparison[/bold]\n")

    detail_table = Table()
    detail_table.add_column("Target", style="cyan")
    detail_table.add_column("Human", style="yellow")
    detail_table.add_column("Agent", style="green")
    detail_table.add_column("Diff", style="magenta")
    detail_table.add_column("Verdict", style="dim")

    for c in sorted(comparison, key=lambda x: abs(x['difference']), reverse=True):
        diff_str = f"{c['difference']:+.1f}"
        if abs(c['difference']) > 10:
            diff_str = f"[red]{diff_str}[/red]"

        verdict_str = "✓" if c['verdict_match'] else f"[red]{c['human_verdict']} vs {c['agent_verdict']}[/red]"

        detail_table.add_row(
            c['target_id'][:8],
            f"{c['human_score']:.1f}",
            f"{c['agent_score']:.1f}",
            diff_str,
            verdict_str,
        )

    console.print(detail_table)

    return comparison


def generate_report(comparison: list, output_path: Path):
    """Generate calibration report."""
    if not comparison:
        console.print("[yellow]No comparison data for report[/yellow]")
        return

    diffs = [c['difference'] for c in comparison]
    abs_diffs = [abs(d) for d in diffs]

    report = [
        "# Calibration Report",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## Summary",
        "",
        f"- **Targets compared:** {len(comparison)}",
        f"- **Average difference:** {sum(diffs)/len(diffs):+.1f} points",
        f"- **Average absolute difference:** {sum(abs_diffs)/len(abs_diffs):.1f} points",
        f"- **Max absolute difference:** {max(abs_diffs):.1f} points",
        "",
        "## Calibration Status",
        "",
    ]

    target_met = sum(abs_diffs)/len(abs_diffs) < 10
    if target_met:
        report.append("**PASS**: Average divergence is below 10 points target.")
    else:
        report.append("**FAIL**: Average divergence exceeds 10 points target. Prompt tuning required.")

    report.extend([
        "",
        "## Detailed Results",
        "",
        "| Target | Human | Agent | Diff | Verdict Match |",
        "|--------|-------|-------|------|---------------|",
    ])

    for c in comparison:
        match = "✓" if c['verdict_match'] else "✗"
        report.append(f"| {c['target_id'][:8]} | {c['human_score']:.1f} | {c['agent_score']:.1f} | {c['difference']:+.1f} | {match} |")

    report.extend([
        "",
        "## Recommendations",
        "",
    ])

    # Add recommendations based on results
    if not target_met:
        report.append("1. Review criterion definitions and anchors")
        report.append("2. Add more explicit scoring guidance in prompts")
        report.append("3. Consider adding few-shot examples")

    # Write report
    output_path.write_text("\n".join(report))
    console.print(f"[green]Report saved to {output_path}[/green]")


def main():
    parser = argparse.ArgumentParser(
        description='Calibration workflow for scoring agent'
    )
    parser.add_argument('command', choices=['export', 'compare', 'report'],
                       help='Command to run')
    parser.add_argument('--count', type=int, default=10,
                       help='Number of targets to export (default: 10)')
    parser.add_argument('--human', type=str,
                       help='Path to human scores CSV')
    parser.add_argument('--output', type=str,
                       help='Output path for export/report')

    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA_PATH, help="Scorecard JSON")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    schema_path = args.schema

    if args.command == 'export':
        output_path = Path(args.output) if args.output else Path(__file__).parent.parent / 'data' / 'calibration_targets.csv'
        output_path.parent.mkdir(exist_ok=True)

        with get_db() as db:
            export_for_calibration(db, args.count, output_path, schema_path)

    elif args.command == 'compare':
        if not args.human:
            console.print("[red]--human path required for compare command[/red]")
            sys.exit(1)

        human_path = Path(args.human)
        if not human_path.exists():
            console.print(f"[red]Human scores file not found: {human_path}[/red]")
            sys.exit(1)

        with get_db() as db:
            comparison = compare_scores(db, human_path, schema_path)

        # Optionally generate report
        if comparison and args.output:
            generate_report(comparison, Path(args.output))

    elif args.command == 'report':
        console.print("[yellow]Use 'compare --output' to generate report[/yellow]")


if __name__ == '__main__':
    main()
