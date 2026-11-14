#!/usr/bin/env python3
"""Rank the bundled synthetic registry with no database or network connection."""

import argparse
import csv
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.run_stage0_ranking import CRITERION_IDS, rank_targets
from src.scrapers.pretor import PretorParser


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "registry.csv"
AS_OF = date(2026, 7, 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Optional CSV output path")
    args = parser.parse_args()

    registry = PretorParser()
    targets = [
        {**target.model_dump(mode="python"), "id": target.source_id}
        for target in registry.parse_file(EXAMPLE)
    ]
    ranked = rank_targets(targets, AS_OF)
    print(f"{len(ranked)} of {registry.get_stats()['total_rows']} sample plants qualified.")
    print("Rank  Plant                  Capacity      Score")
    for row in ranked:
        print(
            f"{row['rank']:>4}  {row['plant_name']:<20} "
            f"{float(row['capacity_kw']):>7,.0f} kW  {row['normalized_score']:>5.1f}/100"
        )

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8", newline="") as output:
            criteria = CRITERION_IDS
            writer = csv.writer(output)
            writer.writerow(["rank", "plant", "capacity_kw", "normalized_score", *criteria])
            for row in ranked:
                writer.writerow([
                    row["rank"], row["plant_name"], row["capacity_kw"], row["normalized_score"],
                    *(row["points"][criterion] for criterion in criteria),
                ])


if __name__ == "__main__":
    main()
