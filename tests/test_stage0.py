"""
Tests for the Stage-0 deterministic ranking module (src/scoring/stage0.py).

Pure-Python: no DB, no network. Uses a fixed AS_OF date for reproducibility
and drift-guards criterion ids/points against examples/scorecard.json.
"""

from datetime import date

from src.scoring.schema import DEFAULT_SCHEMA_PATH, load_schema
from src.scoring.stage0 import (
    CRITERIA,
    STAGE0_MAX_POINTS,
    classify_owner_name,
    compute_stage0,
    score_contracted_tenure,
    score_curtailment,
    score_export_poi_value,
    score_inverter_life,
    score_irradiation_yield,
    score_offtaker_credit,
    score_regime_bankability,
    score_regulatory_clawback,
    score_repower_headroom,
    score_repower_permit_headroom,
    score_tariff_vs_market,
)

AS_OF = date(2026, 7, 1)


def row(**kwargs) -> dict:
    """A typical 2008-vintage RECORE row, overridable per test."""
    base = {
        'id': 'test-id',
        'plant_name': 'EXAMPLE SOLAR FIXTURE 1',
        'capacity_kw': 5000.0,
        'commissioning_date': date(2008, 6, 1),
        'province': 'MURCIA',
        'region': 'MURCIA',
        'tariff_regime': 'RECORE',
        'tariff_rate_eur_kwh': None,
        'fit_remaining_years': None,
        'curtailment_risk': 'high',
    }
    base.update(kwargs)
    return base


# =============================================================================
# Drift guard vs scorecard schema
# =============================================================================

class TestSchemaDriftGuard:
    def test_criterion_ids_and_points_match_schema(self):
        schema = load_schema(DEFAULT_SCHEMA_PATH)
        screening = schema.get_screening_criteria()
        schema_map = {c.id: c.max_points for c in screening}
        stage0_map = {cid: pts for cid, pts, _ in CRITERIA}
        assert stage0_map == schema_map

    def test_max_points_sum(self):
        schema = load_schema(DEFAULT_SCHEMA_PATH)
        assert sum(pts for _, pts, _ in CRITERIA) == STAGE0_MAX_POINTS
        assert schema.screening_max_points() == STAGE0_MAX_POINTS
        assert schema.validate() == []
        assert schema.total_points == 100


# =============================================================================
# Per-criterion anchors
# =============================================================================

class TestRegimeBankability:
    def test_recore(self):
        assert score_regime_bankability(row(), AS_OF)[0] == 6

    def test_unknown(self):
        assert score_regime_bankability(row(tariff_regime=None), AS_OF)[0] == 0

    def test_other_regime(self):
        assert score_regime_bankability(row(tariff_regime='merchant'), AS_OF)[0] == 0


class TestContractedTenure:
    def test_2008_vintage_has_over_5y_left(self):
        # 2008 + 30y life = 2038; ~12y remaining at AS_OF.
        assert score_contracted_tenure(row(), AS_OF)[0] == 5

    def test_fit_remaining_years_takes_precedence(self):
        assert score_contracted_tenure(row(fit_remaining_years=2.0), AS_OF)[0] == 2

    def test_near_expiry(self):
        # 1998 + 30y life = 2028 -> ~1.9y remaining at AS_OF.
        r = row(commissioning_date=date(1998, 6, 1))
        assert score_contracted_tenure(r, AS_OF)[0] == 2

    def test_expired(self):
        r = row(commissioning_date=date(1990, 1, 1))
        assert score_contracted_tenure(r, AS_OF)[0] == 0

    def test_null_commissioning(self):
        pts, reason = score_contracted_tenure(row(commissioning_date=None), AS_OF)
        assert pts == 0
        assert 'unknown' in reason


class TestTariffVsMarket:
    def test_null_rate_scores_zero(self):
        pts, reason = score_tariff_vs_market(row(), AS_OF)
        assert pts == 0
        assert 'unknown' in reason

    def test_rich_fit_tariff(self):
        assert score_tariff_vs_market(row(tariff_rate_eur_kwh=0.30), AS_OF)[0] == 4

    def test_at_market(self):
        assert score_tariff_vs_market(row(tariff_rate_eur_kwh=0.05), AS_OF)[0] == 2


class TestOfftakerCredit:
    def test_recore(self):
        assert score_offtaker_credit(row(), AS_OF)[0] == 3

    def test_unknown(self):
        assert score_offtaker_credit(row(tariff_regime=None), AS_OF)[0] == 0


class TestRegulatoryClawback:
    def test_small_plant(self):
        assert score_regulatory_clawback(row(capacity_kw=3000.0), AS_OF)[0] == 4

    def test_mid_plant(self):
        assert score_regulatory_clawback(row(capacity_kw=9000.0), AS_OF)[0] == 3

    def test_cap_plant(self):
        assert score_regulatory_clawback(row(capacity_kw=10000.0), AS_OF)[0] == 2

    def test_null_commissioning(self):
        assert score_regulatory_clawback(row(commissioning_date=None), AS_OF)[0] == 2

    def test_null_capacity(self):
        assert score_regulatory_clawback(row(capacity_kw=None), AS_OF)[0] == 0


class TestExportPoiValue:
    def test_never_exceeds_4_without_poi_data(self):
        r = row(capacity_kw=10000.0, curtailment_risk='high')
        assert score_export_poi_value(r, AS_OF)[0] == 4

    def test_small_plant_no_congestion(self):
        r = row(capacity_kw=1000.0, curtailment_risk='none')
        assert score_export_poi_value(r, AS_OF)[0] == 2

    def test_no_data(self):
        r = row(capacity_kw=None, curtailment_risk=None)
        assert score_export_poi_value(r, AS_OF)[0] == 0


class TestRepowerHeadroom:
    def test_2008(self):
        assert score_repower_headroom(row(), AS_OF)[0] == 5

    def test_2013(self):
        r = row(commissioning_date=date(2013, 6, 1))
        assert score_repower_headroom(r, AS_OF)[0] == 4

    def test_2018(self):
        r = row(commissioning_date=date(2018, 6, 1))
        assert score_repower_headroom(r, AS_OF)[0] == 2

    def test_null(self):
        assert score_repower_headroom(row(commissioning_date=None), AS_OF)[0] == 0


class TestCurtailment:
    def test_none(self):
        assert score_curtailment(row(curtailment_risk='none'), AS_OF)[0] == 4

    def test_moderate(self):
        assert score_curtailment(row(curtailment_risk='moderate'), AS_OF)[0] == 2

    def test_high(self):
        assert score_curtailment(row(curtailment_risk='high'), AS_OF)[0] == 1

    def test_case_and_whitespace_insensitive(self):
        assert score_curtailment(row(curtailment_risk='HIGH '), AS_OF)[0] == 1

    def test_null(self):
        assert score_curtailment(row(curtailment_risk=None), AS_OF)[0] == 0


class TestIrradiationYield:
    def test_murcia_band_4(self):
        assert score_irradiation_yield(row(province='MURCIA'), AS_OF)[0] == 4

    def test_accented_province(self):
        assert score_irradiation_yield(row(province='Cáceres'), AS_OF)[0] == 4

    def test_valladolid_band_2(self):
        assert score_irradiation_yield(row(province='VALLADOLID'), AS_OF)[0] == 2

    def test_region_fallback(self):
        r = row(province='UNKNOWNVILLE', region='EXTREMADURA')
        assert score_irradiation_yield(r, AS_OF)[0] == 4

    def test_region_fallback_underscored(self):
        r = row(province=None, region='CASTILLA_LA_MANCHA')
        assert score_irradiation_yield(r, AS_OF)[0] == 3

    def test_unknown(self):
        assert score_irradiation_yield(row(province=None, region=None), AS_OF)[0] == 0


class TestInverterLife:
    def test_13y_old_in_window(self):
        r = row(commissioning_date=date(2013, 6, 1))  # ~13y at AS_OF
        assert score_inverter_life(r, AS_OF)[0] == 4

    def test_2008_vintage_just_past_window(self):
        # 2008-06-01 is ~18.1y at AS_OF -> just past the 15-18 band -> 2.
        assert score_inverter_life(row(), AS_OF)[0] == 2

    def test_17y_old_in_late_band(self):
        r = row(commissioning_date=date(2009, 6, 1))  # ~17y at AS_OF
        assert score_inverter_life(r, AS_OF)[0] == 3

    def test_young_plant(self):
        r = row(commissioning_date=date(2022, 6, 1))
        assert score_inverter_life(r, AS_OF)[0] == 2

    def test_null(self):
        assert score_inverter_life(row(commissioning_date=None), AS_OF)[0] == 0


class TestRepowerPermitHeadroom:
    def test_never_awards_3(self):
        for cap in (100.0, 5000.0, 9000.0, 10000.0):
            assert score_repower_permit_headroom(row(capacity_kw=cap), AS_OF)[0] <= 2

    def test_under_threshold(self):
        assert score_repower_permit_headroom(row(capacity_kw=3000.0), AS_OF)[0] == 2

    def test_at_cap(self):
        assert score_repower_permit_headroom(row(capacity_kw=10000.0), AS_OF)[0] == 1

    def test_null(self):
        assert score_repower_permit_headroom(row(capacity_kw=None), AS_OF)[0] == 0


# =============================================================================
# Monotonicity (all else equal)
# =============================================================================

class TestMonotonicity:
    def total(self, **kwargs) -> int:
        return compute_stage0(row(**kwargs), as_of=AS_OF).total

    def test_sunny_province_beats_plateau(self):
        assert self.total(province='MURCIA') >= self.total(province='VALLADOLID')

    def test_small_plant_beats_cap_plant(self):
        assert self.total(capacity_kw=3000.0) >= self.total(capacity_kw=10000.0)

    def test_old_vintage_beats_recent(self):
        # 2008 vs 2013 is intentionally NOT monotonic in total (repower favors
        # 2008, inverter-cycle favors 2013); but 2008 must beat post-2015.
        assert self.total(commissioning_date=date(2008, 6, 1)) >= \
            self.total(commissioning_date=date(2018, 6, 1))

    def test_repower_criterion_monotonic_in_vintage(self):
        assert score_repower_headroom(row(commissioning_date=date(2008, 6, 1)), AS_OF)[0] >= \
            score_repower_headroom(row(commissioning_date=date(2013, 6, 1)), AS_OF)[0] >= \
            score_repower_headroom(row(commissioning_date=date(2018, 6, 1)), AS_OF)[0]

    def test_no_congestion_beats_high(self):
        assert self.total(curtailment_risk='none') >= self.total(curtailment_risk='high')


# =============================================================================
# compute_stage0 aggregate
# =============================================================================

class TestComputeStage0:
    def test_all_criteria_present(self):
        result = compute_stage0(row(), as_of=AS_OF)
        assert set(result.points) == {cid for cid, _, _ in CRITERIA}
        assert set(result.reasons) == set(result.points)

    def test_total_and_normalized(self):
        result = compute_stage0(row(), as_of=AS_OF)
        assert result.total == sum(result.points.values())
        assert result.normalized == round(result.total / STAGE0_MAX_POINTS * 100, 1)
        assert 0 < result.total <= STAGE0_MAX_POINTS

    def test_all_null_row_scores_zero(self):
        empty = {k: None for k in row()}
        result = compute_stage0(empty, as_of=AS_OF)
        assert result.total == 0

    def test_points_never_exceed_max(self):
        result = compute_stage0(row(), as_of=AS_OF)
        for cid, max_pts, _ in CRITERIA:
            assert 0 <= result.points[cid] <= max_pts


# =============================================================================
# Owner-name classification (informational only)
# =============================================================================

class TestClassifyOwnerName:
    def test_entity(self):
        assert classify_owner_name('SOCIEDAD FICTICIA EJEMPLO S.L.') == 'entity'

    def test_plant_label(self):
        assert classify_owner_name('EXAMPLE SOLAR FIXTURE 1') == 'plant_label'

    def test_person(self):
        assert classify_owner_name('PERSONA FICTICIA EJEMPLO') == 'person'

    def test_blank(self):
        assert classify_owner_name(None) == 'blank'
        assert classify_owner_name('  ') == 'blank'
