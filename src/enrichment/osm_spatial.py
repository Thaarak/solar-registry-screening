"""
Municipality-scoped spatial matching between OSM solar plant polygons and PRETOR targets.

Strategy (Solution 1 for vulnerabilities 2.1/2.2):
- PRETOR names rarely match OSM names (name matching yielded 2.8%), but every
  target has a municipality. Query Overpass for power=plant + plant:source=solar
  polygons *inside* each target municipality, then assign polygons to targets
  using capacity disambiguation (OSM capacity tag, or polygon-area-derived
  capacity estimate).
- Only municipalities with few targets are attempted (default <=3); dense
  municipalities are skipped because a wrong coordinate is worse than a missing
  one (it poisons the cadastral lookup and location scoring).
"""

import logging
import re
import time
from dataclasses import dataclass, field
from math import cos, radians
from typing import Optional

import requests

logger = logging.getLogger(__name__)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Plausible land use for 2008-2013 Spanish PV (fixed tilt and trackers):
# roughly 8-60 m2/kW, with 12-35 m2/kW typical.
AREA_PER_KW_TYPICAL = (12.0, 35.0)
AREA_PER_KW_PLAUSIBLE = (8.0, 60.0)

# Province names as stored in our DB (uppercase, no accents) -> ISO 3166-2 codes.
# Exact ISO tag lookup is far faster in Overpass than name-regex over all areas.
# MURCIA uses the uniprovincial community code (ES-MC), which reliably exists.
PROVINCE_ISO_CODES = {
    'ALBACETE': 'ES-AB',
    'ALMERIA': 'ES-AL',
    'AVILA': 'ES-AV',
    'BADAJOZ': 'ES-BA',
    'BURGOS': 'ES-BU',
    'CACERES': 'ES-CC',
    'CADIZ': 'ES-CA',
    'CIUDAD REAL': 'ES-CR',
    'CORDOBA': 'ES-CO',
    'CUENCA': 'ES-CU',
    'GRANADA': 'ES-GR',
    'GUADALAJARA': 'ES-GU',
    'HUELVA': 'ES-H',
    'JAEN': 'ES-J',
    'LEON': 'ES-LE',
    'MALAGA': 'ES-MA',
    'MURCIA': 'ES-MC',
    'PALENCIA': 'ES-P',
    'SALAMANCA': 'ES-SA',
    'SEGOVIA': 'ES-SG',
    'SEVILLA': 'ES-SE',
    'SORIA': 'ES-SO',
    'TOLEDO': 'ES-TO',
    'VALLADOLID': 'ES-VA',
    'ZAMORA': 'ES-ZA',
}

# PRETOR style "Puebla de Montalbán (La)" -> OSM style "La Puebla de Montalbán"
ARTICLE_PATTERN = re.compile(
    r"^(?P<base>.+?)\s*\((?P<art>La|El|Los|Las|L'|Es|Sa|A|O|As|Os|Els|Les)\)\s*$",
    re.IGNORECASE,
)


@dataclass
class OSMPlantPolygon:
    """A power=plant solar element from OSM with derived geometry info."""
    osm_id: str            # e.g. "way/123456"
    name: Optional[str]
    latitude: float        # centroid
    longitude: float
    capacity_kw: Optional[float]   # from plant:output:electricity tag
    area_m2: Optional[float]       # from polygon geometry (None for nodes)
    area_method: Optional[str]     # 'shoelace' | 'bbox' | None
    tags: dict


@dataclass
class SpatialMatch:
    """Assignment of an OSM polygon to a PRETOR target."""
    target_id: str
    target_name: Optional[str]
    target_capacity_kw: Optional[float]
    municipality: str
    province: str
    osm_plant: OSMPlantPolygon
    confidence: float
    method: str            # 'single_pair' | 'capacity_tag' | 'capacity_area'
    details: dict = field(default_factory=dict)


# =============================================================================
# Name handling
# =============================================================================

def municipality_name_candidates(name: str) -> list[str]:
    """Generate OSM name candidates for a PRETOR municipality name."""
    name = name.strip()
    candidates = [name]

    m = ARTICLE_PATTERN.match(name)
    if m:
        art, base = m.group('art'), m.group('base').strip()
        if art.endswith("'"):
            candidates.insert(0, f"{art}{base}")   # L'Alcúdia
        else:
            candidates.insert(0, f"{art} {base}")  # La Puebla de Montalbán
        candidates.append(base)                    # bare name as last resort

    # Dedupe preserving order
    seen, out = set(), []
    for c in candidates:
        if c.lower() not in seen:
            seen.add(c.lower())
            out.append(c)
    return out


def _regex_escape(name: str) -> str:
    """Escape regex specials only (re.escape escapes spaces, which Overpass rejects)."""
    return re.sub(r'([.^$*+?()\[\]{}|\\])', r'\\\1', name)


def _regex_alt(names: list[str]) -> str:
    """Build an anchored alternation regex for Overpass."""
    escaped = [_regex_escape(n) for n in names]
    return f"^({'|'.join(escaped)})$"


def build_municipality_query(municipality: str, province: str, timeout: int = 90) -> Optional[str]:
    """Build Overpass QL for solar power=plant elements inside a municipality."""
    iso_code = PROVINCE_ISO_CODES.get(province.strip().upper())
    if not iso_code:
        logger.warning(f"No ISO code mapping for province {province!r}")
        return None

    muni_regex = _regex_alt(municipality_name_candidates(municipality))

    return f"""
[out:json][timeout:{timeout}];
area["ISO3166-2"="{iso_code}"]->.prov;
rel["boundary"="administrative"]["admin_level"="8"]["name"~"{muni_regex}",i](area.prov);
map_to_area->.muni;
(
  way["power"="plant"]["plant:source"="solar"](area.muni);
  relation["power"="plant"]["plant:source"="solar"](area.muni);
);
out geom;
"""


# =============================================================================
# Geometry
# =============================================================================

def _ring_area_m2(coords: list[dict]) -> float:
    """Shoelace area of a lat/lon ring using local equirectangular projection."""
    if len(coords) < 3:
        return 0.0
    lat0 = sum(c['lat'] for c in coords) / len(coords)
    mx = 111320.0 * cos(radians(lat0))  # meters per degree lon
    my = 110540.0                       # meters per degree lat

    area = 0.0
    n = len(coords)
    for i in range(n):
        x1, y1 = coords[i]['lon'] * mx, coords[i]['lat'] * my
        x2, y2 = coords[(i + 1) % n]['lon'] * mx, coords[(i + 1) % n]['lat'] * my
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


def _centroid(points: list[dict]) -> tuple[float, float]:
    lat = sum(p['lat'] for p in points) / len(points)
    lon = sum(p['lon'] for p in points) / len(points)
    return lat, lon


def parse_overpass_plants(data: dict) -> list[OSMPlantPolygon]:
    """Parse `out geom` Overpass response into OSMPlantPolygon objects."""
    plants = []
    for el in data.get('elements', []):
        el_type = el.get('type')
        tags = el.get('tags', {})
        osm_id = f"{el_type}/{el.get('id')}"

        points: list[dict] = []
        area_m2 = None
        area_method = None

        if el_type == 'way':
            points = el.get('geometry') or []
            if len(points) >= 3:
                area_m2 = _ring_area_m2(points)
                area_method = 'shoelace'
        elif el_type == 'relation':
            for member in el.get('members', []):
                points.extend(member.get('geometry') or [])
            if len(points) >= 3:
                # Multipolygon rings are split across members; use bbox as a
                # rough over-estimate rather than reassembling rings.
                lats = [p['lat'] for p in points]
                lons = [p['lon'] for p in points]
                lat0 = sum(lats) / len(lats)
                area_m2 = (
                    (max(lats) - min(lats)) * 110540.0
                    * (max(lons) - min(lons)) * 111320.0 * cos(radians(lat0))
                )
                area_method = 'bbox'
        elif el_type == 'node':
            if 'lat' in el and 'lon' in el:
                points = [{'lat': el['lat'], 'lon': el['lon']}]

        if not points:
            continue

        lat, lon = _centroid(points)

        cap_str = tags.get('plant:output:electricity') or ''
        capacity_kw = _parse_capacity_kw(cap_str)

        plants.append(OSMPlantPolygon(
            osm_id=osm_id,
            name=tags.get('name'),
            latitude=lat,
            longitude=lon,
            capacity_kw=capacity_kw,
            area_m2=area_m2,
            area_method=area_method,
            tags=tags,
        ))
    return plants


def _parse_capacity_kw(value: str) -> Optional[float]:
    """Parse OSM capacity strings like '5 MW', '1.2MWp', '850 kW' to kW."""
    if not value:
        return None
    m = re.match(r'\s*([\d.,]+)\s*(GW|MW|KW|W)?', value.strip(), re.IGNORECASE)
    if not m:
        return None
    try:
        number = float(m.group(1).replace(',', '.'))
    except ValueError:
        return None
    unit = (m.group(2) or 'KW').upper()
    factor = {'GW': 1e6, 'MW': 1e3, 'KW': 1.0, 'W': 1e-3}[unit]
    return number * factor


# =============================================================================
# Overpass client (municipality-scoped)
# =============================================================================

class MunicipalityOverpassClient:
    """Queries solar power=plant elements per municipality, with retries."""

    def __init__(self, delay_seconds: float = 1.5, max_retries: int = 3, timeout: int = 90):
        self.delay_seconds = delay_seconds
        self.max_retries = max_retries
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'SolarAcquisitionPlatform/1.0 (research project)',
            'Accept': 'application/json',
        })
        self.stats = {'requests': 0, 'success': 0, 'failed': 0, 'no_mapping': 0}

    def query_municipality(self, municipality: str, province: str) -> Optional[dict]:
        """Return raw Overpass JSON for a municipality, or None on failure."""
        query = build_municipality_query(municipality, province, timeout=self.timeout)
        if query is None:
            self.stats['no_mapping'] += 1
            return None

        for attempt in range(self.max_retries):
            self.stats['requests'] += 1
            if attempt > 0:
                wait = self.delay_seconds * (2 ** attempt)
                logger.info(f"Retry {attempt + 1}/{self.max_retries} for {municipality}, waiting {wait:.0f}s")
                time.sleep(wait)
            try:
                resp = self.session.post(
                    OVERPASS_URL, data={'data': query}, timeout=self.timeout + 30
                )
                if resp.status_code == 200:
                    self.stats['success'] += 1
                    return resp.json()
                logger.warning(f"HTTP {resp.status_code} for {municipality} (attempt {attempt + 1})")
            except requests.RequestException as e:
                logger.warning(f"Request error for {municipality} (attempt {attempt + 1}): {e}")

        self.stats['failed'] += 1
        return None

    def get_stats(self) -> dict:
        return self.stats.copy()


# =============================================================================
# Matching
# =============================================================================

def _capacity_tag_score(target_kw: float, osm_kw: float) -> Optional[float]:
    """Score agreement between target capacity and OSM capacity tag. None = reject."""
    if target_kw <= 0 or osm_kw <= 0:
        return None
    ratio = min(target_kw, osm_kw) / max(target_kw, osm_kw)
    if ratio >= 0.8:
        return 1.0
    if ratio >= 0.6:
        return 0.85
    if ratio >= 0.4:
        return 0.6
    return None  # wildly different -> likely a different plant


def _capacity_area_score(target_kw: float, area_m2: float, method: str) -> Optional[float]:
    """Score plausibility of polygon area for the target capacity. None = reject."""
    if target_kw <= 0 or not area_m2:
        return None
    m2_per_kw = area_m2 / target_kw
    lo_t, hi_t = AREA_PER_KW_TYPICAL
    lo_p, hi_p = AREA_PER_KW_PLAUSIBLE
    if method == 'bbox':
        hi_t, hi_p = hi_t * 1.6, hi_p * 1.6  # bbox over-estimates area
    if lo_t <= m2_per_kw <= hi_t:
        return 1.0
    if lo_p <= m2_per_kw <= hi_p:
        return 0.7
    return None


def _pair_score(target_kw: Optional[float], plant: OSMPlantPolygon) -> tuple[Optional[float], str]:
    """
    Score a (target, polygon) pair using the best available evidence.

    Returns (score, method); score None means the pair is rejected or has no
    capacity evidence (method 'no_info').
    """
    if target_kw and plant.capacity_kw:
        return _capacity_tag_score(target_kw, plant.capacity_kw), 'capacity_tag'
    if target_kw and plant.area_m2:
        return _capacity_area_score(target_kw, plant.area_m2, plant.area_method or 'shoelace'), 'capacity_area'
    return None, 'no_info'


class SpatialMatcher:
    """
    Assigns OSM polygons to targets within one municipality.

    Rules:
    - 1 target, 1 polygon: match unless capacity evidence rejects it.
      Confidence 0.75 without evidence, up to 0.95 with corroboration.
    - Multiple targets/polygons: greedy one-to-one assignment by capacity
      score; pairs without capacity evidence are never auto-assigned; if two
      targets score within `ambiguity_margin` for the same polygon, skip it.
    """

    def __init__(self, confidence_threshold: float = 0.70, ambiguity_margin: float = 0.05):
        self.confidence_threshold = confidence_threshold
        self.ambiguity_margin = ambiguity_margin
        self.stats = {
            'munis_processed': 0,
            'matched': 0,
            'rejected_capacity': 0,
            'ambiguous_skipped': 0,
            'no_polygons': 0,
        }

    def match_municipality(
        self,
        targets: list[dict],
        plants: list[OSMPlantPolygon],
        municipality: str,
        province: str,
    ) -> list[SpatialMatch]:
        """targets: dicts with id, plant_name, capacity_kw (float or None)."""
        self.stats['munis_processed'] += 1

        if not plants:
            self.stats['no_polygons'] += 1
            return []

        if len(targets) == 1 and len(plants) == 1:
            return self._match_single_pair(targets[0], plants[0], municipality, province)

        return self._match_multi(targets, plants, municipality, province)

    def _match_single_pair(self, target, plant, municipality, province) -> list[SpatialMatch]:
        target_kw = float(target['capacity_kw']) if target['capacity_kw'] else None
        score, method = _pair_score(target_kw, plant)

        if method != 'no_info' and score is None:
            # Capacity evidence actively contradicts the pairing
            self.stats['rejected_capacity'] += 1
            return []

        confidence = 0.75 if method == 'no_info' else min(0.95, 0.75 + 0.2 * score)
        if confidence < self.confidence_threshold:
            return []

        self.stats['matched'] += 1
        return [SpatialMatch(
            target_id=str(target['id']),
            target_name=target['plant_name'],
            target_capacity_kw=target_kw,
            municipality=municipality,
            province=province,
            osm_plant=plant,
            confidence=confidence,
            method='single_pair' if method == 'no_info' else method,
            details={'score': score, 'osm_capacity_kw': plant.capacity_kw,
                     'osm_area_m2': plant.area_m2},
        )]

    def _match_multi(self, targets, plants, municipality, province) -> list[SpatialMatch]:
        # Score all pairs with capacity evidence
        pairs = []  # (score, target_idx, plant_idx, method)
        for ti, t in enumerate(targets):
            target_kw = float(t['capacity_kw']) if t['capacity_kw'] else None
            for pi, p in enumerate(plants):
                score, method = _pair_score(target_kw, p)
                if method == 'no_info':
                    continue  # never auto-assign in multi case without evidence
                if score is None:
                    self.stats['rejected_capacity'] += 1
                    continue
                pairs.append((score, ti, pi, method))

        pairs.sort(key=lambda x: -x[0])

        matches = []
        used_targets, used_plants = set(), set()

        for score, ti, pi, method in pairs:
            if ti in used_targets or pi in used_plants:
                continue

            # Ambiguity check: another unassigned target nearly as good for this polygon?
            rivals = [
                s for s, ti2, pi2, _ in pairs
                if pi2 == pi and ti2 != ti and ti2 not in used_targets
            ]
            if rivals and max(rivals) >= score - self.ambiguity_margin:
                self.stats['ambiguous_skipped'] += 1
                used_plants.add(pi)  # poison this polygon; don't give it to the rival either
                continue

            confidence = 0.45 + 0.5 * score
            if confidence < self.confidence_threshold:
                continue

            used_targets.add(ti)
            used_plants.add(pi)
            t, p = targets[ti], plants[pi]
            self.stats['matched'] += 1
            matches.append(SpatialMatch(
                target_id=str(t['id']),
                target_name=t['plant_name'],
                target_capacity_kw=float(t['capacity_kw']) if t['capacity_kw'] else None,
                municipality=municipality,
                province=province,
                osm_plant=p,
                confidence=confidence,
                method=method,
                details={'score': score, 'osm_capacity_kw': p.capacity_kw,
                         'osm_area_m2': p.area_m2,
                         'n_targets': len(targets), 'n_polygons': len(plants)},
            ))

        return matches

    def get_stats(self) -> dict:
        return self.stats.copy()
