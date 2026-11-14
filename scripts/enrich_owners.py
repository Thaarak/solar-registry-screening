#!/usr/bin/env python3
"""
Populate the `owners` table for a fixed set of targets (§R.4, zero-spend).

Two modes:

  Seed (default) -- deterministic, no network. Classifies each target's
  `plant_name` via `classify_owner_name` and writes an owner row
  (owner_name / owner_type / approachability, data_source='PLANT_NAME').

  Ingest -- fold verified contact details (email / phone / registered
  address / NIF-CIF / director) from a JSON file back into the owner rows.
  Used by the layer-2 web-search resolution pass.

Usage:
    python scripts/enrich_owners.py --ids-file data/outreach_top25_ids.txt
    python scripts/enrich_owners.py --ids <uuid,uuid,...>
    python scripts/enrich_owners.py --ingest-contacts data/owner_contacts.json
    python scripts/enrich_owners.py --ids-file ... --dry-run

Ingest JSON shape (list of objects, one per target):
    [
      {
        "target_id": "uuid",
        "owner_name": "Example Solar Holdings",   # optional override
        "email": "owner@example.com",
        "phone": "+34 ...",
        "registered_address": "...",
        "nif_cif": "EXAMPLE-TAX-ID",
        "director_name": "...",
        "data_source": "WEB",                              # provenance tag
        "notes": "..."                                     # optional, ignored by DB
      }
    ]
"""

import argparse
import json
import logging
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db import get_db
from src.scoring.stage0 import classify_owner_name

console = Console()
logger = logging.getLogger(__name__)

# plant_name classification -> (owner_type, approachability)
# owner_type stays UNKNOWN unless the name is structurally a company (SME);
# we do not infer occupation (farmer vs private) from a bare person name.
CLASS_MAP = {
    "person":      ("UNKNOWN", "HIGH"),    # a named individual, direct contact
    "entity":      ("SME",     "MEDIUM"),  # small S.L./S.A. owning a single asset
    "plant_label": ("UNKNOWN", "LOW"),     # descriptive label, owner not in name
    "blank":       ("UNKNOWN", "UNKNOWN"),
    "other":       ("UNKNOWN", "LOW"),
}

CONTACT_FIELDS = (
    "email", "phone", "registered_address", "nif_cif",
    "director_name", "director_title", "pec_email",
)


def read_ids(args) -> list[str]:
    if args.ids:
        raw = args.ids
    elif args.ids_file:
        raw = Path(args.ids_file).read_text()
    else:
        raise SystemExit("provide --ids or --ids-file")
    return [x.strip() for x in raw.replace("\n", ",").split(",") if x.strip()]


def load_targets(db, ids: list[str]) -> list[dict]:
    rows = db.execute(
        text(
            """
            SELECT id, plant_name, municipality, province
            FROM targets
            WHERE id = ANY(CAST(:ids AS uuid[]))
            """
        ),
        {"ids": ids},
    ).fetchall()
    by_id = {str(r._mapping["id"]): dict(r._mapping) for r in rows}
    # preserve caller order
    return [
        {**by_id[i], "id": i}
        for i in ids
        if i in by_id
    ]


def seed_owner(db, target_id: str, plant_name: str) -> tuple[str, str, str]:
    """Insert/refresh an owner row from the plant_name. Returns the mapping used."""
    cls = classify_owner_name(plant_name)
    owner_type, approachability = CLASS_MAP.get(cls, ("UNKNOWN", "LOW"))
    owner_name = (plant_name or "").strip() or "(unknown)"
    db.execute(
        text(
            """
            INSERT INTO owners (
                target_id, owner_name, owner_type, approachability,
                data_source, data_source_date
            ) VALUES (
                :target_id, :owner_name, :owner_type, :approachability,
                'PLANT_NAME', NOW()
            )
            ON CONFLICT (target_id, owner_name) DO UPDATE SET
                owner_type = EXCLUDED.owner_type,
                approachability = EXCLUDED.approachability,
                updated_at = NOW()
            """
        ),
        {
            "target_id": target_id,
            "owner_name": owner_name,
            "owner_type": owner_type,
            "approachability": approachability,
        },
    )
    return cls, owner_type, approachability


def ingest_contact(db, rec: dict) -> bool:
    """Fold verified contact fields into the owner row for a target. Update-only."""
    tid = rec.get("target_id")
    if not tid:
        logger.warning("Skipping contact record with no target_id")
        return False

    sets = []
    params = {"target_id": tid}
    if rec.get("owner_name"):
        sets.append("owner_name = :owner_name")
        params["owner_name"] = rec["owner_name"].strip()
    if rec.get("owner_type"):
        sets.append("owner_type = :owner_type")
        params["owner_type"] = rec["owner_type"].strip().upper()
    if rec.get("approachability"):
        sets.append("approachability = :approachability")
        params["approachability"] = rec["approachability"].strip().upper()
    for field in CONTACT_FIELDS:
        if rec.get(field):
            sets.append(f"{field} = :{field}")
            params[field] = str(rec[field]).strip()
    if rec.get("data_source"):
        sets.append("data_source = :data_source")
        params["data_source"] = rec["data_source"]
    if not sets:
        return False
    sets.append("data_source_date = NOW()")
    sets.append("updated_at = NOW()")

    result = db.execute(
        text(f"UPDATE owners SET {', '.join(sets)} WHERE target_id = :target_id"),
        params,
    )
    if result.rowcount == 0:
        logger.warning("no owner row for target %s (seed it first)", tid)
        return False
    return True


def main():
    parser = argparse.ArgumentParser(description="Populate owners table (zero-spend)")
    parser.add_argument("--ids", help="comma-separated target UUIDs")
    parser.add_argument("--ids-file", help="file of comma/newline-separated UUIDs")
    parser.add_argument("--ingest-contacts", help="JSON file of verified contacts to fold in")
    parser.add_argument("--dry-run", action="store_true", help="show plan, no DB writes")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )

    if args.ingest_contacts:
        run_ingest(args)
    else:
        run_seed(args)


def run_seed(args):
    ids = read_ids(args)
    console.print(f"[bold cyan]Owner seed[/bold cyan] — {len(ids)} targets\n")

    with get_db() as db:
        targets = load_targets(db, ids)
        missing = set(ids) - {t["id"] for t in targets}
        if missing:
            console.print(f"[yellow]{len(missing)} id(s) not found in targets[/yellow]")

        table = Table(title="Seeded owners")
        table.add_column("plant_name", style="white", max_width=42)
        table.add_column("class", style="cyan")
        table.add_column("owner_type", style="green")
        table.add_column("approach", style="magenta")

        counts = {}
        for t in targets:
            cls = classify_owner_name(t["plant_name"])
            counts[cls] = counts.get(cls, 0) + 1
            if not args.dry_run:
                cls, otype, appr = seed_owner(db, t["id"], t["plant_name"])
            else:
                otype, appr = CLASS_MAP.get(cls, ("UNKNOWN", "LOW"))
            table.add_row((t["plant_name"] or "")[:42], cls, otype, appr)

        if not args.dry_run:
            db.commit()

        console.print(table)
        console.print(f"\n[dim]class distribution: {counts}[/dim]")
        if args.dry_run:
            console.print("[yellow]dry-run — no rows written[/yellow]")
        else:
            total = db.execute(text("SELECT COUNT(*) FROM owners")).scalar()
            console.print(f"[bold green]owners table now has {total} rows[/bold green]")


def run_ingest(args):
    records = json.loads(Path(args.ingest_contacts).read_text())
    console.print(f"[bold cyan]Contact ingest[/bold cyan] — {len(records)} records\n")

    updated = 0
    with get_db() as db:
        for rec in records:
            if args.dry_run:
                console.print(f"  would update {rec.get('target_id')}: "
                              f"{[k for k in rec if k not in ('target_id','notes')]}")
                continue
            if ingest_contact(db, rec):
                updated += 1
        if not args.dry_run:
            db.commit()

    console.print(f"\n[bold green]updated {updated}/{len(records)} owner rows[/bold green]"
                  if not args.dry_run else "[yellow]dry-run — no writes[/yellow]")


if __name__ == "__main__":
    main()
