"""
Screening memo generator for scored targets.

Generates 3-page screening memos in Markdown format with:
1. Executive Summary
2. Scorecard Breakdown
3. Outstanding DD Checklist
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from .evaluation_agent import ScoringResult, TargetData
from .schema import ScorecardSchema

logger = logging.getLogger(__name__)


class MemoGenerator:
    """Generate screening memos for scored targets."""

    def __init__(self, schema: ScorecardSchema):
        self.schema = schema

    def generate_memo(
        self,
        target: TargetData,
        result: ScoringResult,
        output_path: Optional[Path] = None,
    ) -> str:
        """Generate a screening memo for a scored target."""
        memo = []

        # Header
        memo.append(self._generate_header(target, result))

        # Executive Summary
        memo.append(self._generate_executive_summary(target, result))

        # Scorecard Breakdown
        memo.append(self._generate_scorecard_breakdown(target, result))

        # Outstanding DD Checklist
        memo.append(self._generate_dd_checklist(target, result))

        # Footer
        memo.append(self._generate_footer(result))

        content = "\n\n".join(memo)

        if output_path:
            output_path.write_text(content)
            logger.info(f"Memo saved to: {output_path}")

        return content

    def _generate_header(self, target: TargetData, result: ScoringResult) -> str:
        """Generate memo header."""
        verdict_emoji = {
            'PURSUE': '🟢',
            'CONDITIONAL': '🟡',
            'PASS': '🔴',
        }

        lines = [
            f"# Screening Memo: {target.plant_name or 'Unknown Plant'}",
            "",
            f"**Target ID:** {target.id}",
            f"**Date:** {datetime.now().strftime('%Y-%m-%d')}",
            f"**Verdict:** {verdict_emoji.get(result.verdict, '')} **{result.verdict}** ({result.total_score:.1f}/100)",
            "",
            "---",
        ]
        return "\n".join(lines)

    def _generate_executive_summary(self, target: TargetData, result: ScoringResult) -> str:
        """Generate executive summary section."""
        lines = [
            "## Executive Summary",
            "",
        ]

        # Key metrics
        lines.append("### Key Metrics")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|--------|-------|")
        lines.append(f"| Capacity | {target.capacity_kw:.0f} kW |" if target.capacity_kw else "| Capacity | Unknown |")

        if target.commissioning_date:
            age_years = (datetime.now().date() - target.commissioning_date).days / 365.25
            lines.append(f"| Commissioned | {target.commissioning_date} ({age_years:.1f} years) |")
        else:
            lines.append("| Commissioned | Unknown |")

        lines.append(f"| Location | {target.municipality or 'Unknown'}, {target.province or 'Unknown'} |")
        lines.append(f"| Tariff Regime | {target.tariff_regime or 'Unknown'} |")
        lines.append(f"| Total Score | {result.total_score:.1f}/100 |")
        lines.append(f"| Confidence | {result.overall_confidence:.0%} |")
        lines.append("")

        # Verdict rationale
        lines.append("### Verdict Rationale")
        lines.append("")

        if result.verdict == 'PURSUE':
            lines.append(
                f"This target scores **{result.total_score:.1f} points**, exceeding the PURSUE threshold of "
                f"{self.schema.verdict_thresholds['pursue']} points. "
                "Recommended for active engagement."
            )
        elif result.verdict == 'CONDITIONAL':
            lines.append(
                f"This target scores **{result.total_score:.1f} points**, falling in the CONDITIONAL range "
                f"({self.schema.verdict_thresholds['conditional']}-{self.schema.verdict_thresholds['pursue']}). "
                "Additional due diligence required before engagement decision."
            )
        else:
            lines.append(
                f"This target scores **{result.total_score:.1f} points**, below the CONDITIONAL threshold of "
                f"{self.schema.verdict_thresholds['conditional']}. "
                "Not recommended for pursuit at this time."
            )

        # Deal-breakers
        if not result.gates_passed:
            lines.append("")
            lines.append("**Deal-Breaker Flags:**")
            for gate_id in result.failed_gates:
                gate = next((g for g in self.schema.deal_breakers if g.id == gate_id), None)
                if gate:
                    lines.append(f"- ⚠️ {gate.name}: {gate.description}")

        # Key strengths and risks
        lines.append("")
        lines.append("### Key Strengths")
        lines.append("")
        strengths = self._identify_strengths(result)
        for strength in strengths[:3]:
            lines.append(f"- ✅ {strength}")

        lines.append("")
        lines.append("### Key Risks")
        lines.append("")
        risks = self._identify_risks(result)
        for risk in risks[:3]:
            lines.append(f"- ⚠️ {risk}")

        return "\n".join(lines)

    def _generate_scorecard_breakdown(self, target: TargetData, result: ScoringResult) -> str:
        """Generate scorecard breakdown section."""
        lines = [
            "## Scorecard Breakdown",
            "",
        ]

        # Category summary table
        lines.append("### Category Summary")
        lines.append("")
        lines.append("| Category | Score | Max | % |")
        lines.append("|----------|-------|-----|---|")

        category_scores = [
            ("Revenue & Contract", result.revenue_contract_score, 22),
            ("Grid & Repowering", result.grid_repowering_score, 22),
            ("Resource & Performance", result.resource_performance_score, 18),
            ("Technical Condition", result.technical_condition_score, 16),
            ("Land & Permitting", result.land_permitting_score, 12),
            ("Commercial", result.commercial_score, 10),
        ]

        for name, score, max_score in category_scores:
            pct = (score / max_score * 100) if max_score > 0 else 0
            bar = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
            lines.append(f"| {name} | {score:.1f} | {max_score} | {bar} {pct:.0f}% |")

        lines.append(f"| **TOTAL** | **{result.total_score:.1f}** | **100** | |")
        lines.append("")

        # Detailed criterion breakdown by category
        lines.append("### Detailed Scores")
        lines.append("")

        for category in self.schema.categories:
            lines.append(f"#### {category.name}")
            lines.append("")
            lines.append("| Criterion | Score | Confidence | Evidence |")
            lines.append("|-----------|-------|------------|----------|")

            for criterion in category.criteria:
                cs = next(
                    (s for s in result.criteria_scores if s.criterion_id == criterion.id),
                    None
                )
                if cs:
                    # Truncate evidence for table
                    evidence = cs.evidence[:60] + "..." if len(cs.evidence) > 60 else cs.evidence
                    confidence_emoji = {
                        'HIGH': '🟢',
                        'MEDIUM': '🟡',
                        'LOW': '🟠',
                        'UNKNOWN': '❓',
                    }
                    lines.append(
                        f"| {criterion.name} | {cs.score:.1f}/{cs.max_score} | "
                        f"{confidence_emoji.get(cs.confidence, '')} {cs.confidence} | {evidence} |"
                    )
                else:
                    lines.append(f"| {criterion.name} | - | - | Not scored |")

            lines.append("")

        return "\n".join(lines)

    def _generate_dd_checklist(self, target: TargetData, result: ScoringResult) -> str:
        """Generate outstanding DD checklist section."""
        lines = [
            "## Outstanding Due Diligence Checklist",
            "",
            "Items marked with ❓ require additional information before final decision.",
            "",
        ]

        # Group by category
        checklist_items = []

        # Add unknown criteria
        for criterion_id in result.unknown_criteria:
            criterion = self.schema.get_criterion(criterion_id)
            if criterion:
                checklist_items.append({
                    'category': criterion.category,
                    'item': criterion.name,
                    'reason': 'Data unavailable or insufficient',
                    'priority': 'HIGH' if criterion.max_points >= 4 else 'MEDIUM',
                })

        # Add low confidence scores
        for cs in result.criteria_scores:
            if cs.confidence == 'LOW' and cs.criterion_id not in result.unknown_criteria:
                criterion = self.schema.get_criterion(cs.criterion_id)
                if criterion:
                    checklist_items.append({
                        'category': criterion.category,
                        'item': criterion.name,
                        'reason': 'Low confidence score - needs verification',
                        'priority': 'MEDIUM',
                    })

        # Add missing data items
        missing_data_checks = []
        if not target.referencia_catastral:
            missing_data_checks.append(('land_permitting', 'Cadastral Reference', 'Required for land tenure verification'))
        if not target.owner_name:
            missing_data_checks.append(('commercial', 'Owner Identification', 'Required for outreach'))
        if not target.poi_capacity_kw:
            missing_data_checks.append(('grid_repowering', 'POI Capacity', 'Required for repowering assessment'))
        if not target.latitude or not target.longitude:
            missing_data_checks.append(('resource_performance', 'Coordinates', 'Required for solar resource assessment'))

        for cat, item, reason in missing_data_checks:
            checklist_items.append({
                'category': cat,
                'item': item,
                'reason': reason,
                'priority': 'HIGH',
            })

        if not checklist_items:
            lines.append("✅ No outstanding DD items identified.")
            return "\n".join(lines)

        # Group by priority
        lines.append("### High Priority")
        lines.append("")
        high_priority = [i for i in checklist_items if i['priority'] == 'HIGH']
        for item in high_priority:
            lines.append(f"- [ ] **{item['item']}**: {item['reason']}")

        lines.append("")
        lines.append("### Medium Priority")
        lines.append("")
        medium_priority = [i for i in checklist_items if i['priority'] == 'MEDIUM']
        for item in medium_priority:
            lines.append(f"- [ ] **{item['item']}**: {item['reason']}")

        if not high_priority and not medium_priority:
            lines.append("✅ No outstanding DD items identified.")

        return "\n".join(lines)

    def _generate_footer(self, result: ScoringResult) -> str:
        """Generate memo footer."""
        lines = [
            "---",
            "",
            f"*Generated: {result.scored_at.strftime('%Y-%m-%d %H:%M:%S')}*",
            f"*Model: {result.model_version}*",
            f"*Prompt Version: {result.prompt_version}*",
        ]
        return "\n".join(lines)

    def _identify_strengths(self, result: ScoringResult) -> list[str]:
        """Identify key strengths from scoring."""
        strengths = []

        # Find high-scoring criteria
        for cs in sorted(result.criteria_scores, key=lambda x: x.score / x.max_score if x.max_score > 0 else 0, reverse=True):
            if cs.max_score > 0 and cs.score / cs.max_score >= 0.75:
                criterion = self.schema.get_criterion(cs.criterion_id)
                if criterion:
                    strengths.append(f"{criterion.name}: {cs.evidence}")

        return strengths[:5]

    def _identify_risks(self, result: ScoringResult) -> list[str]:
        """Identify key risks from scoring."""
        risks = []

        # Failed gates
        for gate_id in result.failed_gates:
            gate = next((g for g in self.schema.deal_breakers if g.id == gate_id), None)
            if gate:
                risks.append(f"{gate.name}")

        # Low-scoring criteria
        for cs in sorted(result.criteria_scores, key=lambda x: x.score / x.max_score if x.max_score > 0 else 1):
            if cs.max_score > 0 and cs.score / cs.max_score <= 0.25:
                criterion = self.schema.get_criterion(cs.criterion_id)
                if criterion:
                    risks.append(f"{criterion.name}: {cs.evidence}")

        # Unknown criteria
        if result.unknown_criteria:
            risks.append(f"{len(result.unknown_criteria)} criteria could not be assessed due to missing data")

        return risks[:5]


# =============================================================================
# CLI for testing
# =============================================================================

if __name__ == '__main__':
    import sys
    from datetime import date

    logging.basicConfig(level=logging.INFO)

    # Create sample data for testing
    from .schema import DEFAULT_SCHEMA_PATH, load_schema
    from .evaluation_agent import CriterionScore

    schema_path = DEFAULT_SCHEMA_PATH
    schema = load_schema(schema_path)

    # Sample target
    target = TargetData(
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

    # Sample result
    sample_scores = []
    for criterion in schema.criteria:
        # Generate random-ish scores for testing
        import random
        score = random.randint(0, criterion.max_points)
        confidence = random.choice(['HIGH', 'MEDIUM', 'LOW', 'UNKNOWN'])
        sample_scores.append(CriterionScore(
            criterion_id=criterion.id,
            score=score,
            max_score=criterion.max_points,
            confidence=confidence,
            evidence="Sample evidence for testing",
            is_unknown=confidence == 'UNKNOWN',
        ))

    result = ScoringResult(
        target_id=target.id,
        total_score=65.0,
        verdict='CONDITIONAL',
        revenue_contract_score=15.0,
        grid_repowering_score=14.0,
        resource_performance_score=12.0,
        technical_condition_score=10.0,
        land_permitting_score=8.0,
        commercial_score=6.0,
        criteria_scores=sample_scores,
        gates_passed=True,
        failed_gates=[],
        overall_confidence=0.7,
        unknown_criteria=['owner_type', 'approachability'],
        model_version='test',
        prompt_version='1.0',
    )

    # Generate memo
    generator = MemoGenerator(schema)
    memo = generator.generate_memo(target, result)

    print(memo)
