"""
Scorecard schema loader and validator.

Loads the 100-point scorecard schema from JSON and provides
structured access to categories, criteria, and deal-breakers.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)
DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "examples" / "scorecard.json"


@dataclass
class ScoringAnchor:
    """A scoring anchor point with score and description."""
    score: int
    description: str


@dataclass
class Criterion:
    """A single scoring criterion."""
    id: str
    category: str
    name: str
    max_points: int
    description: str
    anchors: dict[int, str]  # score -> description
    data_fields: list[str]
    default_if_missing: Optional[str] = None
    stage: str = "screening"  # "screening" (registry-derivable) or "dd_only"

    def get_anchor_text(self) -> str:
        """Format anchors for prompt inclusion."""
        lines = []
        for score in sorted(self.anchors.keys(), reverse=True):
            lines.append(f"  {score} points: {self.anchors[score]}")
        return "\n".join(lines)


@dataclass
class Category:
    """A scoring category containing multiple criteria."""
    id: str
    name: str
    max_points: int
    description: str
    criteria: list[Criterion] = field(default_factory=list)


@dataclass
class DealBreaker:
    """A deal-breaker gate condition."""
    id: str
    name: str
    description: str
    check: str
    data_field: str


@dataclass
class DDWalkAway:
    """A due-diligence-stage walk-away condition (not auto-evaluated at screening)."""
    id: str
    name: str
    description: str
    stage: str = "dd_only"


@dataclass
class ScorecardSchema:
    """Full scorecard schema with categories, criteria, and gates."""
    version: str
    description: str
    total_points: int
    verdict_thresholds: dict[str, int]
    categories: list[Category]
    criteria: list[Criterion]
    deal_breakers: list[DealBreaker]
    dd_walk_aways: list[DDWalkAway] = field(default_factory=list)

    def get_category(self, category_id: str) -> Optional[Category]:
        """Get a category by ID."""
        for cat in self.categories:
            if cat.id == category_id:
                return cat
        return None

    def get_criterion(self, criterion_id: str) -> Optional[Criterion]:
        """Get a criterion by ID."""
        for crit in self.criteria:
            if crit.id == criterion_id:
                return crit
        return None

    def get_criteria_by_category(self, category_id: str) -> list[Criterion]:
        """Get all criteria for a category."""
        return [c for c in self.criteria if c.category == category_id]

    def get_verdict(self, total_score: float) -> str:
        """Determine verdict based on total score."""
        if total_score >= self.verdict_thresholds['pursue']:
            return 'PURSUE'
        elif total_score >= self.verdict_thresholds['conditional']:
            return 'CONDITIONAL'
        else:
            return 'PASS'

    def get_criteria_by_stage(self, stage: str) -> list[Criterion]:
        """Get all criteria for a given stage ('screening' or 'dd_only')."""
        return [c for c in self.criteria if c.stage == stage]

    def get_screening_criteria(self) -> list[Criterion]:
        """Get registry-derivable (screening-stage) criteria."""
        return self.get_criteria_by_stage('screening')

    def get_criteria_for_stage(self, stage: str) -> list[Criterion]:
        """Get the criteria to score for a run stage.

        'screening' -> only screening criteria.
        'dd' or 'full' -> all criteria.
        """
        if stage == 'screening':
            return self.get_screening_criteria()
        return list(self.criteria)

    def screening_max_points(self) -> int:
        """Total max points across screening-stage criteria."""
        return sum(c.max_points for c in self.get_screening_criteria())

    def get_screening_verdict(self, screening_score: float) -> str:
        """Determine verdict from a 0-100 normalized screening score."""
        return self.get_verdict(screening_score)

    def validate(self) -> list[str]:
        """Validate schema integrity, return list of errors."""
        errors = []

        # Check total points match
        category_total = sum(cat.max_points for cat in self.categories)
        if category_total != self.total_points:
            errors.append(
                f"Category points ({category_total}) don't match total ({self.total_points})"
            )

        # Check criteria points match categories
        for cat in self.categories:
            criteria = self.get_criteria_by_category(cat.id)
            criteria_total = sum(c.max_points for c in criteria)
            if criteria_total != cat.max_points:
                errors.append(
                    f"Category '{cat.id}' criteria points ({criteria_total}) "
                    f"don't match category max ({cat.max_points})"
                )

        # Check all criteria have valid categories
        category_ids = {cat.id for cat in self.categories}
        for crit in self.criteria:
            if crit.category not in category_ids:
                errors.append(f"Criterion '{crit.id}' has invalid category '{crit.category}'")

        return errors


def load_schema(schema_path: str | Path) -> ScorecardSchema:
    """Load and parse scorecard schema from JSON file."""
    path = Path(schema_path)

    if not path.exists():
        raise FileNotFoundError(f"Schema file not found: {path}")

    with path.open(encoding="utf-8") as f:
        data = json.load(f)

    # Parse categories
    categories = []
    for cat_data in data.get('categories', []):
        categories.append(Category(
            id=cat_data['id'],
            name=cat_data['name'],
            max_points=cat_data['max_points'],
            description=cat_data.get('description', ''),
        ))

    # Parse criteria
    criteria = []
    for crit_data in data.get('criteria', []):
        # Convert anchor keys from string to int
        anchors = {}
        for score_str, desc in crit_data.get('anchors', {}).items():
            anchors[int(score_str)] = desc

        criteria.append(Criterion(
            id=crit_data['id'],
            category=crit_data['category'],
            name=crit_data['name'],
            max_points=crit_data['max_points'],
            description=crit_data.get('description', ''),
            anchors=anchors,
            data_fields=crit_data.get('data_fields', []),
            default_if_missing=crit_data.get('default_if_missing'),
            stage=crit_data.get('stage', 'screening'),
        ))

    # Parse deal-breakers
    deal_breakers = []
    for db_data in data.get('deal_breakers', []):
        deal_breakers.append(DealBreaker(
            id=db_data['id'],
            name=db_data['name'],
            description=db_data.get('description', ''),
            check=db_data.get('check', ''),
            data_field=db_data.get('data_field', ''),
        ))

    # Parse DD-stage walk-aways (documented, not auto-evaluated at screening)
    dd_walk_aways = []
    for wa_data in data.get('dd_walk_aways', []):
        dd_walk_aways.append(DDWalkAway(
            id=wa_data['id'],
            name=wa_data['name'],
            description=wa_data.get('description', ''),
            stage=wa_data.get('stage', 'dd_only'),
        ))

    # Build schema
    schema = ScorecardSchema(
        version=data.get('version', '1.0'),
        description=data.get('description', ''),
        total_points=data.get('total_points', 100),
        verdict_thresholds=data.get('verdict_thresholds', {
            'pursue': 70,
            'conditional': 50,
            'pass': 0,
        }),
        categories=categories,
        criteria=criteria,
        deal_breakers=deal_breakers,
        dd_walk_aways=dd_walk_aways,
    )

    # Link criteria to categories
    for cat in schema.categories:
        cat.criteria = schema.get_criteria_by_category(cat.id)

    # Validate
    errors = schema.validate()
    if errors:
        for error in errors:
            logger.warning(f"Schema validation warning: {error}")

    logger.info(
        f"Loaded scorecard schema v{schema.version}: "
        f"{len(schema.categories)} categories, {len(schema.criteria)} criteria, "
        f"{len(schema.deal_breakers)} deal-breakers"
    )

    return schema


# =============================================================================
# CLI for testing
# =============================================================================

if __name__ == '__main__':
    import sys

    logging.basicConfig(level=logging.INFO)

    # Default schema path
    schema_path = DEFAULT_SCHEMA_PATH

    if len(sys.argv) > 1:
        schema_path = Path(sys.argv[1])

    print(f"Loading schema from: {schema_path}")

    try:
        schema = load_schema(schema_path)

        print(f"\nSchema: {schema.description}")
        print(f"Version: {schema.version}")
        print(f"Total points: {schema.total_points}")
        print(f"\nVerdict thresholds:")
        for verdict, threshold in schema.verdict_thresholds.items():
            print(f"  {verdict.upper()}: >={threshold}")

        print(f"\nCategories ({len(schema.categories)}):")
        for cat in schema.categories:
            print(f"  - {cat.name}: {cat.max_points} pts ({len(cat.criteria)} criteria)")

        screening = schema.get_screening_criteria()
        print(
            f"\nStage tags: {len(screening)} screening criteria "
            f"({schema.screening_max_points()} pts) / "
            f"{len(schema.get_criteria_by_stage('dd_only'))} dd_only criteria"
        )
        print("  Screening criteria:")
        for crit in screening:
            print(f"    - {crit.id} ({crit.max_points} pts)")

        print(f"\nDeal-breakers ({len(schema.deal_breakers)}):")
        for db in schema.deal_breakers:
            print(f"  - {db.name}")

        if schema.dd_walk_aways:
            print(f"\nDD walk-aways ({len(schema.dd_walk_aways)}):")
            for wa in schema.dd_walk_aways:
                print(f"  - {wa.name}")

        # Validate
        errors = schema.validate()
        if errors:
            print(f"\nValidation errors:")
            for error in errors:
                print(f"  - {error}")
        else:
            print("\nSchema validation: PASSED")

    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
