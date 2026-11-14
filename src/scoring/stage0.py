"""
Stage-0 deterministic ranking.

A free, reproducible pre-rank of all qualified ES targets that mirrors the 11
`screening`-stage scorecard criteria (48 pts) using pure rules over registry
fields — no LLM, no network, no DB writes. The LLM screening pipeline
(`scripts/run_scoring.py --stage screening`) then runs only on the Stage-0
shortlist instead of the entire input dataset.

Design rules:
- Every criterion function has the uniform signature
  ``fn(row: dict, as_of: date) -> tuple[int, str]`` returning (points, reason).
- Unknown/NULL inputs score 0 with an explicit "unknown …" reason — an honest
  floor, identical for every row, so constant-NULL fields do not distort the
  *ranking* (they only lower everyone's absolute score equally).
- ``as_of`` is an explicit parameter so age-based scores are reproducible.
- Points/criterion-ids are drift-guarded against `examples/scorecard.json`
  in `tests/test_stage0.py`.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Optional

from src.enrichment.grid_congestion import normalize_province

# =============================================================================
# Constants
# =============================================================================

STAGE0_MAX_POINTS = 48

# RECORE (RD 413/2014) PV regulatory useful life, from commissioning.
RECORE_USEFUL_LIFE_YEARS = 30

# Coarse Spanish captured-price reference for tariff_vs_market (EUR/kWh).
MARKET_CAPTURE_EUR_KWH = 0.05

# Province -> GHI anchor band, per the irradiation_yield anchors:
#   4: GHI >1800 kWh/m2/yr   3: 1600-1800   2: 1400-1600   1: <1400
# Keys are upper-cased, accent-free (see `normalize_province`). The full
# peninsula and islands are covered.
PROVINCE_GHI_BAND: dict[str, int] = {
    # --- Band 4: southern PV belt ---
    'ALMERIA': 4, 'CADIZ': 4, 'CORDOBA': 4, 'GRANADA': 4, 'HUELVA': 4,
    'JAEN': 4, 'MALAGA': 4, 'SEVILLA': 4,
    'BADAJOZ': 4, 'CACERES': 4,
    'MURCIA': 4,
    'LAS PALMAS': 4, 'SANTA CRUZ DE TENERIFE': 4,
    # --- Band 3: Castilla-La Mancha / Levante / central-east ---
    'ALBACETE': 3, 'CIUDAD REAL': 3, 'CUENCA': 3, 'GUADALAJARA': 3, 'TOLEDO': 3,
    'VALENCIA': 3, 'ALICANTE': 3, 'CASTELLON': 3,
    'MADRID': 3, 'ZARAGOZA': 3, 'TERUEL': 3,
    'BALEARES': 3,
    # --- Band 2: northern plateau / Ebro ---
    'AVILA': 2, 'BURGOS': 2, 'LEON': 2, 'PALENCIA': 2, 'SALAMANCA': 2,
    'SEGOVIA': 2, 'SORIA': 2, 'VALLADOLID': 2, 'ZAMORA': 2,
    'LA RIOJA': 2, 'NAVARRA': 2, 'HUESCA': 2, 'LLEIDA': 2, 'TARRAGONA': 2,
    # --- Band 1: green north ---
    'A CORUNA': 1, 'LUGO': 1, 'OURENSE': 1, 'PONTEVEDRA': 1,
    'ASTURIAS': 1, 'CANTABRIA': 1,
    'BIZKAIA': 1, 'GIPUZKOA': 1, 'ARABA': 1,
    'BARCELONA': 1, 'GIRONA': 1,
}

# Fallback when the province is unrecognised but the region is known.
REGION_GHI_BAND: dict[str, int] = {
    'ANDALUCIA': 4,
    'EXTREMADURA': 4,
    'MURCIA': 4,
    'CASTILLA_LA_MANCHA': 3,
    'CASTILLA_Y_LEON': 2,
}


def _normalize_region(region: Optional[str]) -> Optional[str]:
    """Normalize a region label for REGION_GHI_BAND lookup."""
    key = normalize_province(region)
    if not key:
        return None
    return re.sub(r'[\s\-]+', '_', key)


def _age_years(commissioning_date: date, as_of: date) -> float:
    return (as_of - commissioning_date).days / 365.25


# =============================================================================
# Per-criterion deterministic rules (uniform signature)
# =============================================================================

def score_regime_bankability(row: dict, as_of: date) -> tuple[int, str]:
    regime = (row.get('tariff_regime') or '').strip().upper()
    if regime == 'RECORE':
        return 6, 'RECORE regulated remuneration (RD 413/2014)'
    if regime:
        return 0, f'non-regulated/unrecognised regime ({regime})'
    return 0, 'unknown tariff regime'


def score_contracted_tenure(row: dict, as_of: date) -> tuple[int, str]:
    fit = row.get('fit_remaining_years')
    if fit is not None:
        remaining = float(fit)
        basis = 'registry fit_remaining_years'
    else:
        cd = row.get('commissioning_date')
        if cd is None:
            return 0, 'unknown commissioning date'
        remaining = RECORE_USEFUL_LIFE_YEARS - _age_years(cd, as_of)
        basis = f'{RECORE_USEFUL_LIFE_YEARS}y RECORE regulatory life'
    if remaining >= 5:
        return 5, f'~{remaining:.0f}y remaining ({basis})'
    if remaining >= 3:
        return 4, f'~{remaining:.0f}y remaining ({basis})'
    if remaining >= 1:
        return 2, f'~{remaining:.0f}y remaining ({basis})'
    return 0, f'regulatory life expired/near-expired ({basis})'


def score_tariff_vs_market(row: dict, as_of: date) -> tuple[int, str]:
    rate = row.get('tariff_rate_eur_kwh')
    if rate is None:
        return 0, 'unknown tariff rate'
    ratio = float(rate) / MARKET_CAPTURE_EUR_KWH
    if ratio >= 1.5:
        return 4, f'tariff {ratio:.1f}x market capture'
    if ratio >= 1.1:
        return 3, f'tariff {ratio:.1f}x market capture'
    if ratio >= 0.9:
        return 2, f'tariff ~market capture ({ratio:.1f}x)'
    return 0, f'tariff below market capture ({ratio:.1f}x)'


def score_offtaker_credit(row: dict, as_of: date) -> tuple[int, str]:
    regime = (row.get('tariff_regime') or '').strip().upper()
    if regime == 'RECORE':
        return 3, 'CNMC settlement — regulated sovereign-adjacent counterparty'
    return 0, 'unknown/unregulated off-taker'


def score_regulatory_clawback(row: dict, as_of: date) -> tuple[int, str]:
    regime = (row.get('tariff_regime') or '').strip().upper()
    capacity = row.get('capacity_kw')
    if regime != 'RECORE' or capacity is None:
        return 0, 'unknown regime or capacity'
    if row.get('commissioning_date') is None:
        return 2, 'vintage unknown — clawback exposure unassessable'
    capacity = float(capacity)
    if capacity <= 5000:
        return 4, f'{capacity:.0f} kW — well under 10 MW cap, low reform exposure'
    if capacity <= 9000:
        return 3, f'{capacity:.0f} kW — under 10 MW cap'
    return 2, f'{capacity:.0f} kW — at/near 10 MW cap'


def score_export_poi_value(row: dict, as_of: date) -> tuple[int, str]:
    # No poi_capacity_kw / grid_operator in the registry data: cap at 4 —
    # never award "firm HV export" without evidence.
    capacity = row.get('capacity_kw')
    risk = (row.get('curtailment_risk') or '').strip().lower()
    if capacity is None and not risk:
        return 0, 'no capacity or congestion data'
    points = 2
    parts = ['operating exporter since commissioning — POI exists']
    if capacity is not None and float(capacity) >= 5000:
        points += 1
        parts.append('>=5 MW — likely dedicated MV/HV connection')
    if risk == 'high':
        points += 1
        parts.append('saturated node — existing POI is scarce/valuable')
    return points, '; '.join(parts)


def score_repower_headroom(row: dict, as_of: date) -> tuple[int, str]:
    cd = row.get('commissioning_date')
    if cd is None:
        return 0, 'unknown commissioning date'
    if cd.year < 2012:
        return 5, f'{cd.year} vintage — maximum repower/DC-overbuild upside'
    if cd.year <= 2015:
        return 4, f'{cd.year} vintage — strong repower upside'
    return 2, f'{cd.year} vintage — limited repower delta'


def score_curtailment(row: dict, as_of: date) -> tuple[int, str]:
    risk = (row.get('curtailment_risk') or '').strip().lower()
    if risk == 'none':
        return 4, 'no notable congestion (REE zone classification)'
    if risk == 'moderate':
        return 2, 'moderate congestion zone'
    if risk == 'high':
        return 1, 'high-congestion / saturated zone'
    return 0, 'unknown curtailment risk'


def score_irradiation_yield(row: dict, as_of: date) -> tuple[int, str]:
    province = normalize_province(row.get('province'))
    if province and province in PROVINCE_GHI_BAND:
        band = PROVINCE_GHI_BAND[province]
        return band, f'{province} GHI band {band}/4'
    region = _normalize_region(row.get('region'))
    if region and region in REGION_GHI_BAND:
        band = REGION_GHI_BAND[region]
        return band, f'region {region} GHI band {band}/4 (province fallback)'
    return 0, 'unknown province/region'


def score_inverter_life(row: dict, as_of: date) -> tuple[int, str]:
    cd = row.get('commissioning_date')
    if cd is None:
        return 0, 'unknown commissioning date'
    age = _age_years(cd, as_of)
    if 10 <= age < 15:
        return 4, f'~{age:.0f}y old — replacement cycle priced-in window'
    if 8 <= age < 10 or 15 <= age <= 18:
        return 3, f'~{age:.0f}y old — replacement near/likely'
    return 2, f'~{age:.0f}y old — outside reference window'


def score_repower_permit_headroom(row: dict, as_of: date) -> tuple[int, str]:
    # Consent terms are unknowable from registry data — never award 3.
    capacity = row.get('capacity_kw')
    if capacity is None:
        return 0, 'unknown capacity'
    if float(capacity) <= 9000:
        return 2, 'headroom under 10 MW authorisation threshold'
    return 1, 'at/near 10 MW — repower likely needs re-authorisation'


# (criterion_id, max_points, fn) in scorecard schema order.
CRITERIA: list[tuple[str, int, Callable[[dict, date], tuple[int, str]]]] = [
    ('regime_bankability', 6, score_regime_bankability),
    ('contracted_tenure', 5, score_contracted_tenure),
    ('tariff_vs_market', 4, score_tariff_vs_market),
    ('offtaker_credit', 3, score_offtaker_credit),
    ('regulatory_clawback', 4, score_regulatory_clawback),
    ('export_poi_value', 6, score_export_poi_value),
    ('repower_headroom', 5, score_repower_headroom),
    ('curtailment', 4, score_curtailment),
    ('irradiation_yield', 4, score_irradiation_yield),
    ('inverter_life', 4, score_inverter_life),
    ('repower_permit_headroom', 3, score_repower_permit_headroom),
]


# =============================================================================
# Result computation
# =============================================================================

@dataclass
class Stage0Result:
    """Deterministic Stage-0 score for one target."""
    target_id: str
    points: dict[str, int] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)
    total: int = 0
    normalized: float = 0.0


def compute_stage0(row: dict, as_of: Optional[date] = None) -> Stage0Result:
    """
    Score one target row (plain dict of registry fields) against the 11
    screening criteria. `as_of` defaults to today; pass a fixed date for
    reproducible runs.
    """
    as_of = as_of or date.today()
    result = Stage0Result(target_id=str(row.get('id', '')))
    for criterion_id, max_points, fn in CRITERIA:
        pts, reason = fn(row, as_of)
        pts = max(0, min(pts, max_points))
        result.points[criterion_id] = pts
        result.reasons[criterion_id] = reason
    result.total = sum(result.points.values())
    result.normalized = round(result.total / STAGE0_MAX_POINTS * 100, 1)
    return result


# =============================================================================
# Owner-name classification (informational only — never affects the score)
# =============================================================================

_ENTITY_RE = re.compile(
    r'\b(S\.?L\.?U?|S\.?A\.?U?|SLU|SAU|S\.?COOP\w*|SDAD|SOCIEDAD|SCP|SAT|'
    r'A\.?I\.?E|U\.?T\.?E|C\.?B)\b\.?'
    r'|\b(ENERGIA[S]?|RENOVABLES?|INVERSIONES|PROMOCIONES|GESTION|DESARROLLOS?|'
    r'EXPLOTACIONES|CONSTRUCCIONES|INMOBILIARIA|AGROPECUARIA|COOPERATIVA|'
    r'AYUNTAMIENTO|COMUNIDAD)\b'
)
_PLANT_RE = re.compile(
    r'\b(FV|F\.V|FOTOVOLT\w*|SOLAR\w*|HUERT\w*|PLANTA|PARQUE|CUBIERTA[S]?|'
    r'TEJADO|NAVE[S]?|INSTALACION\w*|POLIGONO|FINCA|PARCELA|SECTOR|FASE)\b|\d'
)
_PERSON_RE = re.compile(r'^[A-ZN]+(?: [A-ZN]+){1,4}$')


def _strip_accents_upper(text: str) -> str:
    table = str.maketrans('ÁÉÍÓÚÀÈÌÒÙÄËÏÖÜÑáéíóúàèìòùäëïöüñº ª', 'AEIOUAEIOUAEIOUNAEIOUAEIOUAEIOUN   ')
    return ' '.join(text.translate(table).upper().split())


def classify_owner_name(plant_name: Optional[str]) -> str:
    """
    Heuristic classification of a PRETOR plant_name (§R.4): does it look like
    a titular (person/entity) or a plant label? Informational CSV column only.
    """
    if not plant_name or not str(plant_name).strip():
        return 'blank'
    name = _strip_accents_upper(str(plant_name))
    if _ENTITY_RE.search(name):
        return 'entity'
    if _PLANT_RE.search(name):
        return 'plant_label'
    if _PERSON_RE.match(name) and len(name.split()) >= 2:
        return 'person'
    return 'other'
