"""
AI-powered evaluation agent for solar plant scoring.

Uses Claude API to evaluate targets against the scorecard criteria.
"""

import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

import anthropic

from .schema import ScorecardSchema, Criterion, DEFAULT_SCHEMA_PATH, load_schema
from ..models import is_solar_pv

logger = logging.getLogger(__name__)

# Default model for scoring
DEFAULT_MODEL = "claude-sonnet-4-5-20250929"

# Rate limiting
DEFAULT_DELAY_SECONDS = 0.5
MAX_RETRIES = 3


@dataclass
class CriterionScore:
    """Score for a single criterion."""
    criterion_id: str
    score: float
    max_score: int
    confidence: str  # HIGH, MEDIUM, LOW, UNKNOWN
    evidence: str
    is_unknown: bool = False


@dataclass
class ScoringResult:
    """Complete scoring result for a target."""
    target_id: str
    total_score: float
    verdict: str  # PURSUE, CONDITIONAL, PASS

    # Category scores
    revenue_contract_score: float
    grid_repowering_score: float
    resource_performance_score: float
    technical_condition_score: float
    land_permitting_score: float
    commercial_score: float

    # Detailed scores
    criteria_scores: list[CriterionScore]

    # Deal-breaker gates
    gates_passed: bool
    failed_gates: list[str]

    # Metadata
    overall_confidence: float  # 0.0 to 1.0
    unknown_criteria: list[str]
    model_version: str
    prompt_version: str

    # Two-stage scoring (Stage 1 = screening triage, Stage 2 = dd/full)
    scoring_stage: str = "screening"  # "screening", "dd", or "full"
    screening_score: float = 0.0  # 0-100 normalized over screening-criteria max
    screening_verdict: str = "PASS"  # verdict derived from screening_score

    scored_at: datetime = field(default_factory=datetime.now)

    def to_db_dict(self) -> dict:
        """Convert to dictionary for database insertion."""
        return {
            'target_id': self.target_id,
            'total_score': self.total_score,
            'verdict': self.verdict,
            'revenue_contract_score': self.revenue_contract_score,
            'grid_repowering_score': self.grid_repowering_score,
            'resource_performance_score': self.resource_performance_score,
            'technical_condition_score': self.technical_condition_score,
            'land_permitting_score': self.land_permitting_score,
            'commercial_structure_score': self.commercial_score,
            'criteria_scores': json.dumps([
                {
                    'criterion_id': cs.criterion_id,
                    'score': cs.score,
                    'max_score': cs.max_score,
                    'confidence': cs.confidence,
                    'evidence': cs.evidence,
                    'is_unknown': cs.is_unknown,
                }
                for cs in self.criteria_scores
            ]),
            'gates_passed': self.gates_passed,
            'failed_gates': json.dumps(self.failed_gates),
            'overall_confidence': self.overall_confidence,
            'unknown_criteria': json.dumps(self.unknown_criteria),
            'model_version': self.model_version,
            'prompt_version': self.prompt_version,
            'scoring_stage': self.scoring_stage,
            'screening_score': self.screening_score,
            'screening_verdict': self.screening_verdict,
            'scored_at': self.scored_at,
        }


@dataclass
class TargetData:
    """Target data for scoring evaluation."""
    id: str
    plant_name: Optional[str] = None
    capacity_kw: Optional[float] = None
    commissioning_date: Optional[date] = None
    technology: Optional[str] = None

    latitude: Optional[float] = None
    longitude: Optional[float] = None
    municipality: Optional[str] = None
    province: Optional[str] = None
    region: Optional[str] = None

    tariff_regime: Optional[str] = None
    tariff_rate_eur_kwh: Optional[float] = None
    tariff_expiry_date: Optional[date] = None
    fit_remaining_years: Optional[float] = None

    grid_operator: Optional[str] = None
    poi_capacity_kw: Optional[float] = None

    # Grid congestion (Bucket-1 coordinate-free enrichment, §3.7)
    curtailment_risk: Optional[str] = None
    grid_congestion_zone: Optional[str] = None

    is_operating: bool = True
    is_qualified: bool = False

    # Cadastral data (if available)
    referencia_catastral: Optional[str] = None

    # Owner data (Phase 2 - may be missing)
    owner_name: Optional[str] = None
    owner_type: Optional[str] = None
    approachability: Optional[str] = None

    @classmethod
    def from_db_row(cls, row: Any) -> 'TargetData':
        """Create from database row (SQLAlchemy Row or dict)."""
        # Convert SQLAlchemy Row to dict for easier access
        if hasattr(row, '_mapping'):
            row = dict(row._mapping)
        elif hasattr(row, '_asdict'):
            row = row._asdict()
        elif not isinstance(row, dict):
            # Try to convert to dict
            row = dict(row)

        def safe_float(val):
            if val is None:
                return None
            if isinstance(val, Decimal):
                return float(val)
            return float(val) if val else None

        def safe_date(val):
            if val is None:
                return None
            if isinstance(val, date):
                return val
            return None

        return cls(
            id=str(row.get('id', '')),
            plant_name=row.get('plant_name'),
            capacity_kw=safe_float(row.get('capacity_kw')),
            commissioning_date=safe_date(row.get('commissioning_date')),
            technology=row.get('technology'),
            latitude=safe_float(row.get('latitude')),
            longitude=safe_float(row.get('longitude')),
            municipality=row.get('municipality'),
            province=row.get('province'),
            region=row.get('region'),
            tariff_regime=row.get('tariff_regime'),
            tariff_rate_eur_kwh=safe_float(row.get('tariff_rate_eur_kwh')),
            tariff_expiry_date=safe_date(row.get('tariff_expiry_date')),
            fit_remaining_years=safe_float(row.get('fit_remaining_years')),
            grid_operator=row.get('grid_operator'),
            poi_capacity_kw=safe_float(row.get('poi_capacity_kw')),
            curtailment_risk=row.get('curtailment_risk'),
            grid_congestion_zone=row.get('grid_congestion_zone'),
            is_operating=row.get('is_operating', True),
            is_qualified=row.get('is_qualified', False),
            referencia_catastral=row.get('referencia_catastral'),
            owner_name=row.get('owner_name'),
            owner_type=row.get('owner_type'),
            approachability=row.get('approachability'),
        )


class EvaluationAgent:
    """AI-powered evaluation agent for solar plant scoring."""

    PROMPT_VERSION = "2.0"

    def __init__(
        self,
        schema: ScorecardSchema,
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        delay_seconds: float = DEFAULT_DELAY_SECONDS,
        stage: str = "screening",
    ):
        self.schema = schema
        self.model = model
        self.delay_seconds = delay_seconds
        # Scoring stage: "screening" scores only registry-derivable criteria;
        # "dd"/"full" score all criteria.
        self.stage = stage
        self.scored_criteria = schema.get_criteria_for_stage(stage)

        # Initialize Anthropic client
        api_key = api_key or os.environ.get('ANTHROPIC_API_KEY')
        if not api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY not found. Set it as environment variable or pass to constructor."
            )
        self.client = anthropic.Anthropic(api_key=api_key)

        self.stats = {
            'scored': 0,
            'failed': 0,
            'api_calls': 0,
        }

    def score_target(self, target: TargetData) -> ScoringResult:
        """Score a single target using Claude API."""
        logger.info("Scoring selected target")

        # Check deal-breaker gates first
        failed_gates = self._check_deal_breakers(target)

        # Build prompt
        prompt = self._build_scoring_prompt(target)

        # Call Claude API
        try:
            response = self._call_claude(prompt)
            result = self._parse_response(target, response, failed_gates)
            self.stats['scored'] += 1
            return result
        except Exception as e:
            logger.error(f"Scoring failed for {target.id}: {e}")
            self.stats['failed'] += 1
            raise

    def _check_deal_breakers(self, target: TargetData) -> list[str]:
        """Check deal-breaker gates, return list of failed gate IDs."""
        failed = []

        for gate in self.schema.deal_breakers:
            if self._evaluate_gate(gate.id, target):
                failed.append(gate.id)
                logger.debug(f"Deal-breaker triggered: {gate.name}")

        return failed

    def _evaluate_gate(self, gate_id: str, target: TargetData) -> bool:
        """Evaluate a single deal-breaker gate. Returns True if gate fails."""
        if gate_id == 'capacity_exceeded':
            return target.capacity_kw is not None and target.capacity_kw > 10000

        if gate_id == 'not_operating':
            return target.is_operating is False

        if gate_id == 'tariff_expired':
            if target.tariff_expiry_date and target.tariff_regime != 'MERCHANT':
                return target.tariff_expiry_date < date.today()
            return False

        if gate_id == 'capacity_too_small':
            return target.capacity_kw is not None and target.capacity_kw < 100

        if gate_id == 'outside_spain':
            if target.latitude is not None and target.longitude is not None:
                return not (35.0 <= target.latitude <= 44.0 and -10.0 <= target.longitude <= 5.0)
            return False

        if gate_id == 'not_solar':
            if target.technology:
                return not is_solar_pv(target.technology)
            return False  # permissive on missing technology (PV-strict + permissive null)

        if gate_id == 'not_registered':
            return False  # All our targets come from registries

        if gate_id == 'wrong_country':
            return False  # We're only processing Spain

        return False

    def _build_scoring_prompt(self, target: TargetData) -> str:
        """Build the scoring prompt for Claude."""
        # Format target data
        target_section = self._format_target_data(target)

        # Format criteria (filtered to the criteria in scope for this stage)
        criteria_section = self._format_criteria()

        n_criteria = len(self.scored_criteria)
        example_id = self.scored_criteria[0].id if self.scored_criteria else "criterion_id"

        if self.stage == "screening":
            stage_note = (
                "This is a STAGE-1 SCREENING pass. You are scoring only the "
                "registry-derivable criteria used to triage and rank targets "
                "before due diligence."
            )
        else:
            stage_note = (
                "This is a STAGE-2 (due-diligence) pass. You are scoring the "
                "full scorecard using data-room evidence."
            )

        prompt = f"""You are a solar asset acquisition analyst evaluating a target solar plant for potential acquisition.

{stage_note}

Your task is to score this target against each criterion in the scorecard. For each criterion:
1. Assign a score from 0 to the maximum points based on the scoring anchors
2. Assess your confidence: HIGH (clear data supports score), MEDIUM (some inference), LOW (limited data), UNKNOWN (cannot assess)
3. Provide brief evidence/reasoning for the score

## TARGET DATA

{target_section}

## SCORING CRITERIA

{criteria_section}

## RESPONSE FORMAT

Return a JSON object with this structure:
```json
{{
  "criteria_scores": [
    {{
      "criterion_id": "{example_id}",
      "score": 4,
      "confidence": "MEDIUM",
      "evidence": "RECORE regime but no expiry date provided"
    }}
    // ... all {n_criteria} criteria listed above
  ],
  "overall_assessment": "Brief 2-3 sentence summary of the target's strengths and weaknesses"
}}
```

IMPORTANT:
- Score ALL {n_criteria} criteria listed above (and only those)
- If data is missing for a criterion, use your best judgment or mark confidence as UNKNOWN
- Be conservative with scores when data is limited
- Reference specific data points in your evidence

Return ONLY the JSON object, no additional text."""

        return prompt

    def _format_target_data(self, target: TargetData) -> str:
        """Format target data for prompt."""
        lines = []

        lines.append(f"\n**Capacity & Technology**")
        lines.append(f"- Capacity: {target.capacity_kw:.2f} kW" if target.capacity_kw else "- Capacity: Not provided")
        lines.append(f"- Technology: {target.technology or 'Not provided'}")

        lines.append(f"\n**Commissioning & Tariff**")
        if target.commissioning_date:
            age_years = (date.today() - target.commissioning_date).days / 365.25
            lines.append(f"- Commissioning Date: {target.commissioning_date} ({age_years:.1f} years old)")
        else:
            lines.append(f"- Commissioning Date: Not provided")
        lines.append(f"- Tariff Regime: {target.tariff_regime or 'Not provided'}")
        lines.append(f"- Tariff Rate: {target.tariff_rate_eur_kwh} EUR/kWh" if target.tariff_rate_eur_kwh else "- Tariff Rate: Not provided")
        lines.append(f"- Tariff Expiry: {target.tariff_expiry_date or 'Not provided'}")
        lines.append(f"- FIT Remaining Years: {target.fit_remaining_years:.1f}" if target.fit_remaining_years else "- FIT Remaining Years: Not provided")

        lines.append(f"\n**Location**")
        lines.append(f"- Province: {target.province or 'Not provided'}")
        lines.append(f"- Region: {target.region or 'Not provided'}")
        lines.append(f"\n**Grid Connection**")
        lines.append(f"- Grid Operator: {target.grid_operator or 'Not provided'}")
        lines.append(f"- POI Capacity: {target.poi_capacity_kw:.2f} kW" if target.poi_capacity_kw else "- POI Capacity: Not provided")

        lines.append(f"\n**Grid Congestion**")
        lines.append(f"- Curtailment Risk: {target.curtailment_risk or 'Not provided'}")
        lines.append(f"- Grid Congestion Zone: {target.grid_congestion_zone or 'Not provided'}")


        lines.append(f"\n**Owner (if available)**")
        lines.append(f"- Owner Type: {target.owner_type or 'Not provided'}")

        lines.append(f"\n**Status**")
        lines.append(f"- Operating: {'Yes' if target.is_operating else 'No'}")
        lines.append(f"- Qualified: {'Yes' if target.is_qualified else 'No'}")

        return "\n".join(lines)

    def _format_criteria(self) -> str:
        """Format scoring criteria for prompt (only criteria in scope for the stage)."""
        lines = []
        in_scope = {c.id for c in self.scored_criteria}

        for category in self.schema.categories:
            cat_criteria = [c for c in category.criteria if c.id in in_scope]
            if not cat_criteria:
                continue

            lines.append(f"### {category.name}")
            lines.append("")

            for criterion in cat_criteria:
                lines.append(f"**{criterion.id}**: {criterion.name} (max {criterion.max_points} pts)")
                lines.append(f"{criterion.description}")
                lines.append("Scoring anchors:")
                lines.append(criterion.get_anchor_text())
                lines.append("")

        return "\n".join(lines)

    def _call_claude(self, prompt: str) -> str:
        """Call Claude API with retry logic."""
        for attempt in range(MAX_RETRIES):
            try:
                self.stats['api_calls'] += 1

                if attempt > 0:
                    wait_time = self.delay_seconds * (2 ** attempt)
                    logger.info(f"Retry {attempt + 1}/{MAX_RETRIES}, waiting {wait_time}s...")
                    time.sleep(wait_time)

                message = self.client.messages.create(
                    model=self.model,
                    max_tokens=4096,
                    messages=[
                        {"role": "user", "content": prompt}
                    ]
                )

                # Extract text from response
                response_text = message.content[0].text
                return response_text

            except anthropic.RateLimitError:
                logger.warning(f"Rate limited on attempt {attempt + 1}")
                if attempt == MAX_RETRIES - 1:
                    raise

            except anthropic.APIError as e:
                logger.error(f"API error on attempt {attempt + 1}: {e}")
                if attempt == MAX_RETRIES - 1:
                    raise

        raise Exception("Max retries exceeded")

    def _parse_response(
        self,
        target: TargetData,
        response: str,
        failed_gates: list[str],
    ) -> ScoringResult:
        """Parse Claude's response into ScoringResult."""
        # Extract JSON from response
        try:
            # Try to find JSON in response
            json_start = response.find('{')
            json_end = response.rfind('}') + 1
            if json_start >= 0 and json_end > json_start:
                json_str = response[json_start:json_end]
                data = json.loads(json_str)
            else:
                raise ValueError("No JSON found in response")
        except json.JSONDecodeError as e:
            logger.error(f"JSON parse error: {e}")
            raise ValueError(f"Could not parse response as JSON: {e}")

        # Parse criterion scores
        criteria_scores = []
        unknown_criteria = []

        seen = set()
        for score_data in data.get('criteria_scores', []):
            criterion_id = score_data.get('criterion_id')
            criterion = self.schema.get_criterion(criterion_id)

            if criterion not in self.scored_criteria or criterion_id in seen:
                logger.warning("Ignoring an out-of-scope or duplicate criterion")
                continue
            seen.add(criterion_id)

            score = float(score_data.get('score', 0))
            if not math.isfinite(score):
                raise ValueError("Non-finite criterion score")
            confidence = score_data.get('confidence', 'UNKNOWN')
            evidence = score_data.get('evidence', '')

            # Clamp score to valid range
            score = max(0, min(score, criterion.max_points))

            is_unknown = confidence == 'UNKNOWN'
            if is_unknown:
                unknown_criteria.append(criterion_id)

            criteria_scores.append(CriterionScore(
                criterion_id=criterion_id,
                score=score,
                max_score=criterion.max_points,
                confidence=confidence,
                evidence=evidence,
                is_unknown=is_unknown,
            ))

        # Calculate category scores
        category_scores = {}
        for category in self.schema.categories:
            cat_criteria = [cs for cs in criteria_scores if cs.criterion_id in
                          [c.id for c in category.criteria]]
            category_scores[category.id] = sum(cs.score for cs in cat_criteria)

        # Calculate total score (raw sum of all scored criteria)
        total_score = sum(category_scores.values())

        # Calculate normalized Stage-1 screening score (0-100 over screening-criteria max).
        # This is used for ranking/triage on sparse data, where the absolute
        # 100-point verdict would compress every target toward PASS.
        screening_ids = {c.id for c in self.schema.get_screening_criteria()}
        screening_raw = sum(cs.score for cs in criteria_scores if cs.criterion_id in screening_ids)
        screening_max = self.schema.screening_max_points()
        screening_score = (screening_raw / screening_max * 100) if screening_max else 0.0

        # Calculate overall confidence
        confidence_map = {'HIGH': 1.0, 'MEDIUM': 0.7, 'LOW': 0.4, 'UNKNOWN': 0.1}
        confidence_values = [confidence_map.get(cs.confidence, 0.5) for cs in criteria_scores]
        overall_confidence = sum(confidence_values) / len(confidence_values) if confidence_values else 0.5

        # Determine verdicts
        gates_passed = len(failed_gates) == 0
        screening_verdict = self.schema.get_screening_verdict(screening_score)

        if not gates_passed:
            # Auto-fail if a deal-breaker gate triggered
            verdict = 'PASS'
            screening_verdict = 'PASS'
        elif self.stage == 'screening':
            # Stage 1: the headline verdict is the normalized triage verdict.
            # Absolute PURSUE/PASS is deferred to Stage 2 on enriched data.
            verdict = screening_verdict
        else:
            # Stage 2 (dd/full): absolute verdict over the full 100-point scorecard.
            verdict = self.schema.get_verdict(total_score)

        return ScoringResult(
            target_id=target.id,
            total_score=total_score,
            verdict=verdict,
            revenue_contract_score=category_scores.get('revenue_contract', 0),
            grid_repowering_score=category_scores.get('grid_repowering', 0),
            resource_performance_score=category_scores.get('resource_performance', 0),
            technical_condition_score=category_scores.get('technical_condition', 0),
            land_permitting_score=category_scores.get('land_permitting', 0),
            commercial_score=category_scores.get('commercial', 0),
            criteria_scores=criteria_scores,
            gates_passed=gates_passed,
            failed_gates=failed_gates,
            overall_confidence=overall_confidence,
            unknown_criteria=unknown_criteria,
            model_version=self.model,
            prompt_version=self.PROMPT_VERSION,
            scoring_stage=self.stage,
            screening_score=screening_score,
            screening_verdict=screening_verdict,
        )

    def get_stats(self) -> dict:
        """Return scoring statistics."""
        return self.stats.copy()


# =============================================================================
# CLI for testing
# =============================================================================

if __name__ == '__main__':
    import sys
    from pathlib import Path

    logging.basicConfig(level=logging.INFO)

    # Load schema
    schema_path = DEFAULT_SCHEMA_PATH
    schema = load_schema(schema_path)

    # Create test target
    test_target = TargetData(
        id='test-123',
        plant_name='Planta Solar Ejemplo',
        capacity_kw=2500.0,
        commissioning_date=date(2010, 6, 15),
        technology='Fotovoltaica fija suelo',
        latitude=38.5,
        longitude=-3.5,
        municipality='Villarrobledo',
        province='Albacete',
        region='Castilla-La Mancha',
        tariff_regime='RECORE',
        is_operating=True,
        is_qualified=True,
    )

    print(f"Test target: {test_target.plant_name}")
    print(f"Capacity: {test_target.capacity_kw} kW")
    print(f"Commissioning: {test_target.commissioning_date}")

    # Check if API key is set
    print("\nExample loaded. Use scripts/run_scoring.py for explicit external scoring.")
