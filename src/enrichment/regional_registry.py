"""
Regional registry coordinate enrichment (Solution 1 for vulnerabilities 2.1/2.2).

PRETOR targets carry a "Número de Registro Autonómico Definitivo"
(`regional_registry_id`, ~100% of qualified solar targets). That id is the
deterministic foreign key into the *regional* installation registries published
by Spain's autonomous communities. This module pulls per-installation locations
from those registries and assigns them to targets — preferring the exact
registry-id join, falling back to the same capacity/area safety logic used by
the OSM spatial matcher.

Sources are pluggable (`CoordinateSource`):
- `RegionalFileSource`: bulk static-file registries (e.g. Castilla y León's JCyL
  shapefile). Reachable from any network; this is the only confirmed-working
  source today.
- `RegionalWFSSource`: WFS GetFeature against the regional GIS hosts (Andalucía
  AAE). These hosts are geofenced from some environments; the source fails
  gracefully (returns []) and logs a clear reason, so the same pipeline scales
  the moment it runs from a reachable network (e.g. Spanish egress).

The matcher reuses `osm_spatial`'s density bands and `SpatialMatch` dataclass so
downstream code (audit CSV, DB update, cadastral chaining) is shared verbatim.
"""

import logging
import re
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

from src.enrichment.osm_spatial import (
    OSMPlantPolygon,
    SpatialMatch,
    _capacity_area_score,
    _capacity_tag_score,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Region / province geography
# =============================================================================

# The 5 autonomous communities that hold 100% of the coordinate gap.
PROVINCE_TO_REGION = {
    # Murcia (uniprovincial)
    'MURCIA': 'MURCIA',
    # Castilla-La Mancha
    'ALBACETE': 'CASTILLA_LA_MANCHA',
    'CIUDAD REAL': 'CASTILLA_LA_MANCHA',
    'CUENCA': 'CASTILLA_LA_MANCHA',
    'GUADALAJARA': 'CASTILLA_LA_MANCHA',
    'TOLEDO': 'CASTILLA_LA_MANCHA',
    # Extremadura
    'BADAJOZ': 'EXTREMADURA',
    'CACERES': 'EXTREMADURA',
    # Castilla y León
    'AVILA': 'CASTILLA_Y_LEON',
    'BURGOS': 'CASTILLA_Y_LEON',
    'LEON': 'CASTILLA_Y_LEON',
    'PALENCIA': 'CASTILLA_Y_LEON',
    'SALAMANCA': 'CASTILLA_Y_LEON',
    'SEGOVIA': 'CASTILLA_Y_LEON',
    'SORIA': 'CASTILLA_Y_LEON',
    'VALLADOLID': 'CASTILLA_Y_LEON',
    'ZAMORA': 'CASTILLA_Y_LEON',
    # Andalucía
    'ALMERIA': 'ANDALUCIA',
    'CADIZ': 'ANDALUCIA',
    'CORDOBA': 'ANDALUCIA',
    'GRANADA': 'ANDALUCIA',
    'HUELVA': 'ANDALUCIA',
    'JAEN': 'ANDALUCIA',
    'MALAGA': 'ANDALUCIA',
    'SEVILLA': 'ANDALUCIA',
}

DEFAULT_REGIONS = [
    'MURCIA', 'CASTILLA_LA_MANCHA', 'EXTREMADURA', 'CASTILLA_Y_LEON', 'ANDALUCIA',
]


# =============================================================================
# Source config
# =============================================================================
#
# Each region maps to optional `wfs` and `file` source configs.
#   wfs.endpoint   : WFS base URL
#   wfs.layers     : type names to query (GetFeature typeName=...)
#   wfs.field_map  : RegionalInstallation field -> feature property name
#   file.url       : bulk shapefile/zip download URL
#
# Only the two regions with a real source are listed. Castilla y León's JCyL
# shapefile is the confirmed-working seed. Andalucía's AAE WFS is geofenced and
# pending Spanish egress (see scripts/probe_regional_wfs.py). The remaining gap
# regions (Murcia, Castilla-La Mancha, Extremadura) publish no joinable
# per-installation solar geodata — see IMPLEMENTATION_PLAN.md §1.4.5 for the
# negative-result survey; do not re-add empty placeholders for them.

REGIONAL_SOURCES: dict[str, dict] = {
    'ANDALUCIA': {
        'wfs': {
            'endpoint': 'https://www.agenciaandaluzadelaenergia.es/mapwms/wfs',
            # TODO(egress): confirm exact typeName(s) via GetCapabilities.
            'layers': ['instalaciones_energia_renovable'],
            'field_map': {
                'name': 'nombre',
                'capacity_kw': 'potencia_kw',
                'municipality': 'municipio',
                'regional_ref': 'num_registro',
                'external_id': 'id',
            },
        },
    },
    'CASTILLA_Y_LEON': {
        # Confirmed bulk download: JCyL open-data publishes the regional renewable
        # installation registry as a polygon shapefile (registry code + capacity +
        # geometry). Reachable from this environment; no VPN/geofence. DBF field
        # names are truncated to 10 chars by the shapefile spec.
        'file': {
            'url': 'https://opendata.jcyl.es/ficheros/carto/industria/energia/energia_cyl_renov_instal_polig_shp.zip',
            'source_crs': 'EPSG:25830',  # ETRS89 / UTM zone 30N
            'type_field': 'n_tip_inst',
            'type_filter': 'Solar fotovoltaica',
            'field_map': {
                'regional_ref': 'c_reg_adm',
                'name': 'n_inst_ene',
                'capacity_kw': 'v_kw_tot',
                'municipality': 'n_mun',
                'province': 'n_prov',
            },
        },
    },
}


# =============================================================================
# Data model
# =============================================================================

@dataclass
class RegionalInstallation:
    """A solar installation record sourced from a regional/national registry."""
    source: str                    # e.g. 'CASTILLA_Y_LEON_FILE', 'ANDALUCIA_WFS'
    external_id: Optional[str]
    name: Optional[str]
    latitude: float
    longitude: float
    capacity_kw: Optional[float]
    municipality: Optional[str]
    province: Optional[str]
    regional_ref: Optional[str]    # regional registry id, if the source carries it
    area_m2: Optional[float] = None
    raw: dict = field(default_factory=dict)

    def as_osm_plant(self) -> OSMPlantPolygon:
        """Adapt to the OSMPlantPolygon shape SpatialMatch/audit/update expect."""
        return OSMPlantPolygon(
            osm_id=f"{self.source}/{self.external_id}" if self.external_id else self.source,
            name=self.name,
            latitude=self.latitude,
            longitude=self.longitude,
            capacity_kw=self.capacity_kw,
            area_m2=self.area_m2,
            area_method='shoelace' if self.area_m2 else None,
            tags={'regional_ref': self.regional_ref} if self.regional_ref else {},
        )


def normalize_ref(ref: Optional[str]) -> Optional[str]:
    """
    Normalize a regional registry id for comparison.

    PRETOR refs include parenthetical history, e.g.
    "RGRE/006/08/MA(RGRE/623/05/MA)" -> primary "RGRE/006/08/MA".
    Comparison is case-insensitive and whitespace-insensitive.
    """
    if not ref:
        return None
    primary = ref.split('(')[0]
    cleaned = re.sub(r'\s+', '', primary).upper()
    return cleaned or None


# =============================================================================
# Sources
# =============================================================================

class CoordinateSource(ABC):
    """A pluggable source of installation coordinates, queried per region."""

    name: str = 'base'

    @abstractmethod
    def fetch(self, region: str) -> list[RegionalInstallation]:
        """Return installations for an autonomous community. [] on failure."""
        raise NotImplementedError


class RegionalWFSSource(CoordinateSource):
    """
    WFS GetFeature client against regional GIS hosts (Andalucía AAE, etc.).

    Requests GeoJSON; computes centroid + (optional) area from geometry, and
    maps configured properties to capacity/name/regional_ref. When the host is
    geofenced/unreachable it logs a clear reason and returns [] so the pipeline
    degrades gracefully from this environment and scales under VPN.
    """

    name = 'WFS'

    def __init__(self, timeout: int = 60, max_features: int = 50000):
        self.timeout = timeout
        self.max_features = max_features
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'SolarAcquisitionPlatform/1.0 (research project)',
        })
        self.stats = {'fetched': 0, 'layers': 0, 'errors': 0, 'unreachable': 0}

    def fetch(self, region: str) -> list[RegionalInstallation]:
        cfg = REGIONAL_SOURCES.get(region, {}).get('wfs', {})
        endpoint = cfg.get('endpoint')
        layers = cfg.get('layers', [])
        field_map = cfg.get('field_map', {})

        if not endpoint or not layers:
            logger.info(f"[{self.name}] no WFS endpoint/layers configured for {region} (TODO: confirm under VPN)")
            return []

        installations: list[RegionalInstallation] = []
        source_tag = f"{region}_WFS"
        for layer in layers:
            self.stats['layers'] += 1
            try:
                data = self._get_features(endpoint, layer)
            except requests.exceptions.ConnectionError as e:
                self.stats['unreachable'] += 1
                logger.warning(
                    f"[{self.name}] {region} host unreachable (likely geofenced): {endpoint} ({e})"
                )
                return []
            except (requests.RequestException, ValueError) as e:
                self.stats['errors'] += 1
                logger.warning(f"[{self.name}] {region} layer {layer} failed: {e}")
                continue
            installations.extend(_parse_geojson(data, source_tag, region, field_map))

        self.stats['fetched'] += len(installations)
        return installations

    def _get_features(self, endpoint: str, layer: str) -> dict:
        params = {
            'service': 'WFS',
            'version': '2.0.0',
            'request': 'GetFeature',
            'typeNames': layer,
            'outputFormat': 'application/json',
            'srsName': 'EPSG:4326',
            'count': str(self.max_features),
        }
        resp = self.session.get(endpoint, params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def get_stats(self) -> dict:
        return self.stats.copy()


class RegionalFileSource(CoordinateSource):
    """
    Bulk static-file source for regions that publish their installation registry
    as a downloadable shapefile (vs a geofenced WFS).

    Castilla y León's JCyL open-data portal is the confirmed seed: a polygon
    shapefile carrying the regional registry code (`c_reg_adm`), capacity
    (`v_kw_tot`), municipality and province for every renewable installation.
    The file is fetched (cached on disk), filtered to the configured installation
    type, and each polygon is reduced to a centroid + shoelace area in the
    source CRS, then reprojected to EPSG:4326.

    Regions without a `file` config resolve to [] so this source is a no-op in
    the shared per-region loop.
    """

    name = 'REGIONAL_FILE'

    def __init__(self, timeout: int = 120, cache_dir: Optional[str] = None):
        self.timeout = timeout
        self.cache_dir = Path(cache_dir) if cache_dir else Path(tempfile.gettempdir()) / 'regional_registry_cache'
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'SolarAcquisitionPlatform/1.0 (research project)',
        })
        self.stats = {'fetched': 0, 'filtered_out': 0, 'no_geometry': 0, 'errors': 0}

    def fetch(self, region: str) -> list[RegionalInstallation]:
        cfg = REGIONAL_SOURCES.get(region, {}).get('file', {})
        url = cfg.get('url')
        if not url:
            logger.info(f"[{self.name}] no bulk file configured for {region}")
            return []

        try:
            shp_path = self._download_and_extract(url, region)
        except (requests.RequestException, ValueError, OSError) as e:
            self.stats['errors'] += 1
            logger.warning(f"[{self.name}] {region} download/extract failed: {e}")
            return []

        try:
            return self._read_shapefile(shp_path, region, cfg)
        except (ImportError, ValueError, OSError) as e:
            self.stats['errors'] += 1
            logger.warning(f"[{self.name}] {region} shapefile read failed: {e}")
            return []

    def _download_and_extract(self, url: str, region: str) -> Path:
        """Download the zip (cached) and extract to a region-scoped dir; return .shp path."""
        import zipfile

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        zip_path = self.cache_dir / f"{region}.zip"
        extract_dir = self.cache_dir / region

        if not zip_path.exists():
            logger.info(f"[{self.name}] downloading {url}")
            resp = self.session.get(url, timeout=self.timeout)
            resp.raise_for_status()
            zip_path.write_bytes(resp.content)
        else:
            logger.info(f"[{self.name}] using cached {zip_path}")

        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extract_dir)

        shp_files = list(extract_dir.rglob('*.shp'))
        if not shp_files:
            raise ValueError(f"no .shp found in {url}")
        return shp_files[0]

    def _read_shapefile(self, shp_path: Path, region: str, cfg: dict) -> list[RegionalInstallation]:
        import shapefile  # pyshp, lazy-imported
        from pyproj import Transformer

        field_map = cfg.get('field_map', {})
        type_field = cfg.get('type_field')
        type_filter = cfg.get('type_filter')
        transformer = Transformer.from_crs(cfg['source_crs'], 'EPSG:4326', always_xy=True)
        source_tag = f"{region}_FILE"

        out: list[RegionalInstallation] = []
        reader = shapefile.Reader(str(shp_path), encoding='utf-8')
        for sr in reader.iterShapeRecords():
            props = sr.record.as_dict()

            if type_field and type_filter and props.get(type_field) != type_filter:
                self.stats['filtered_out'] += 1
                continue

            ca = _projected_centroid_area(sr.shape)
            if ca is None:
                self.stats['no_geometry'] += 1
                continue
            cx, cy, area_m2 = ca
            lon, lat = transformer.transform(cx, cy)

            out.append(RegionalInstallation(
                source=source_tag,
                external_id=str(props.get('fid')) if props.get('fid') is not None else None,
                name=_prop(props, field_map, 'name'),
                latitude=lat,
                longitude=lon,
                capacity_kw=_to_float(_prop(props, field_map, 'capacity_kw')),
                municipality=_prop(props, field_map, 'municipality'),
                province=_prop(props, field_map, 'province'),
                regional_ref=_prop(props, field_map, 'regional_ref'),
                area_m2=area_m2 if area_m2 > 0 else None,
                raw=props,
            ))

        self.stats['fetched'] += len(out)
        return out

    def get_stats(self) -> dict:
        return self.stats.copy()


# =============================================================================
# Parsing helpers
# =============================================================================


def _projected_centroid_area(shape) -> Optional[tuple[float, float, float]]:
    """
    Reduce a pyshp polygon to (centroid_x, centroid_y, area) in source CRS units.

    Uses the shoelace formula over each ring; centroid falls back to the vertex
    mean for degenerate (zero-area) rings. Returns None if there are no points.
    """
    points = getattr(shape, 'points', None)
    if not points:
        return None

    parts = list(getattr(shape, 'parts', []) or [0])
    ring_bounds = parts + [len(points)]

    total_area = 0.0
    cx_acc = 0.0
    cy_acc = 0.0
    for start, end in zip(ring_bounds, ring_bounds[1:]):
        ring = points[start:end]
        if len(ring) < 3:
            continue
        a = 0.0
        rx = 0.0
        ry = 0.0
        for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1]):
            cross = x0 * y1 - x1 * y0
            a += cross
            rx += (x0 + x1) * cross
            ry += (y0 + y1) * cross
        a *= 0.5
        if a != 0:
            cx_acc += rx / 6.0
            cy_acc += ry / 6.0
            total_area += a

    if total_area != 0:
        return cx_acc / total_area, cy_acc / total_area, abs(total_area)

    # Degenerate geometry: fall back to vertex mean, zero area.
    n = len(points)
    return sum(p[0] for p in points) / n, sum(p[1] for p in points) / n, 0.0

def _geometry_centroid(geom: dict) -> Optional[tuple[float, float]]:
    """Return (lat, lon) centroid of a GeoJSON geometry; lon/lat input order."""
    coords = _flatten_positions(geom.get('coordinates'))
    if not coords:
        return None
    lon = sum(c[0] for c in coords) / len(coords)
    lat = sum(c[1] for c in coords) / len(coords)
    return lat, lon


def _flatten_positions(coords) -> list[list[float]]:
    """Flatten nested GeoJSON coordinate arrays into a list of [lon, lat]."""
    if not isinstance(coords, list):
        return []
    if coords and isinstance(coords[0], (int, float)):
        return [coords] if len(coords) >= 2 else []
    out: list[list[float]] = []
    for c in coords:
        out.extend(_flatten_positions(c))
    return out


def _to_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(str(value).replace(',', '.'))
    except (TypeError, ValueError):
        return None


def _parse_geojson(data: dict, source: str, region: str, field_map: dict) -> list[RegionalInstallation]:
    out: list[RegionalInstallation] = []
    for feat in data.get('features', []):
        props = feat.get('properties', {}) or {}
        geom = feat.get('geometry') or {}
        centroid = _geometry_centroid(geom)
        if not centroid:
            continue
        lat, lon = centroid
        out.append(RegionalInstallation(
            source=source,
            external_id=_prop(props, field_map, 'external_id') or feat.get('id'),
            name=_prop(props, field_map, 'name'),
            latitude=lat,
            longitude=lon,
            capacity_kw=_to_float(_prop(props, field_map, 'capacity_kw')),
            municipality=_prop(props, field_map, 'municipality'),
            province=_prop(props, field_map, 'province'),
            regional_ref=_prop(props, field_map, 'regional_ref'),
            raw=props,
        ))
    return out


def _prop(props: dict, field_map: dict, field_name: str):
    """Look up a mapped property; fall back to the field name itself."""
    key = field_map.get(field_name, field_name)
    return props.get(key)


# =============================================================================
# Matching
# =============================================================================

class RegistryMatcher:
    """
    Assigns regional installations to PRETOR targets within one province.

    Priority:
      (a) exact `regional_ref` join (normalized) — deterministic, highest conf.
      (b) capacity-tag agreement within a municipality.
      (c) area->capacity density band within a municipality.
    Capacity mismatches are rejected; ambiguous municipality pairs are skipped,
    mirroring `osm_spatial.SpatialMatcher`.
    """

    def __init__(self, confidence_threshold: float = 0.70, ambiguity_margin: float = 0.05):
        self.confidence_threshold = confidence_threshold
        self.ambiguity_margin = ambiguity_margin
        self.stats = {
            'matched_ref': 0,
            'matched_capacity': 0,
            'rejected_capacity': 0,
            'ambiguous_skipped': 0,
            'unmatched': 0,
        }

    def match_province(
        self,
        targets: list[dict],
        installations: list[RegionalInstallation],
        province: str,
    ) -> list[SpatialMatch]:
        """
        targets: dicts with id, plant_name, capacity_kw, regional_registry_id,
                 municipality.
        """
        if not installations:
            self.stats['unmatched'] += len(targets)
            return []

        matches: list[SpatialMatch] = []
        used_install_ids: set[int] = set()
        remaining: list[dict] = []

        # (a) deterministic registry-id join
        ref_index: dict[str, list[int]] = {}
        for i, inst in enumerate(installations):
            nref = normalize_ref(inst.regional_ref)
            if nref:
                ref_index.setdefault(nref, []).append(i)

        for t in targets:
            nref = normalize_ref(t.get('regional_registry_id'))
            cands = [i for i in ref_index.get(nref, []) if i not in used_install_ids] if nref else []
            if len(cands) == 1:
                inst = installations[cands[0]]
                used_install_ids.add(cands[0])
                matches.append(self._make_match(t, inst, province, confidence=0.98, method='regional_ref'))
                self.stats['matched_ref'] += 1
            else:
                remaining.append(t)

        # (b)/(c) municipality-scoped capacity/area fallback for the rest
        free_installs = [(i, inst) for i, inst in enumerate(installations) if i not in used_install_ids]
        matches.extend(self._match_by_capacity(remaining, free_installs, used_install_ids, province))

        return matches

    def _match_by_capacity(self, targets, free_installs, used_install_ids, province) -> list[SpatialMatch]:
        # Group both sides by normalized municipality
        def norm_muni(m):
            return re.sub(r'\s+', ' ', (m or '').strip().lower())

        by_muni_inst: dict[str, list[tuple[int, RegionalInstallation]]] = {}
        for i, inst in free_installs:
            by_muni_inst.setdefault(norm_muni(inst.municipality), []).append((i, inst))

        matches: list[SpatialMatch] = []
        for t in targets:
            muni = norm_muni(t.get('municipality'))
            candidates = by_muni_inst.get(muni, [])
            target_kw = _to_float(t.get('capacity_kw'))

            scored = []  # (score, inst_index, inst, method)
            for i, inst in candidates:
                if i in used_install_ids:
                    continue
                score, method = self._pair_score(target_kw, inst)
                if score is not None:
                    scored.append((score, i, inst, method))

            if not scored:
                self.stats['unmatched'] += 1
                continue

            scored.sort(key=lambda x: -x[0])
            best_score, best_i, best_inst, method = scored[0]

            # Ambiguity: a near-equal runner-up means we can't safely assign.
            if len(scored) > 1 and scored[1][0] >= best_score - self.ambiguity_margin:
                self.stats['ambiguous_skipped'] += 1
                continue

            confidence = 0.45 + 0.5 * best_score
            if confidence < self.confidence_threshold:
                self.stats['unmatched'] += 1
                continue

            used_install_ids.add(best_i)
            self.stats['matched_capacity'] += 1
            matches.append(self._make_match(t, best_inst, province, confidence, method))

        return matches

    def _pair_score(self, target_kw, inst: RegionalInstallation):
        if target_kw and inst.capacity_kw:
            score = _capacity_tag_score(target_kw, inst.capacity_kw)
            if score is None:
                self.stats['rejected_capacity'] += 1
            return score, 'capacity_tag'
        if target_kw and inst.area_m2:
            score = _capacity_area_score(target_kw, inst.area_m2, 'shoelace')
            if score is None:
                self.stats['rejected_capacity'] += 1
            return score, 'capacity_area'
        return None, 'no_info'

    def _make_match(self, target, inst: RegionalInstallation, province, confidence, method) -> SpatialMatch:
        target_kw = _to_float(target.get('capacity_kw'))
        return SpatialMatch(
            target_id=str(target['id']),
            target_name=target.get('plant_name'),
            target_capacity_kw=target_kw,
            municipality=target.get('municipality') or inst.municipality or '',
            province=target.get('province') or province,
            osm_plant=inst.as_osm_plant(),
            confidence=confidence,
            method=method,
            details={
                'source': inst.source,
                'regional_ref': inst.regional_ref,
                'install_capacity_kw': inst.capacity_kw,
            },
        )

    def get_stats(self) -> dict:
        return self.stats.copy()


def make_source(kind: str) -> CoordinateSource:
    """
    Factory:
      'file' -> RegionalFileSource (bulk static downloads, e.g. JCyL shapefile)
      'wfs'  -> RegionalWFSSource (regional GIS hosts; some geofenced)
    """
    if kind == 'wfs':
        return RegionalWFSSource()
    if kind == 'file':
        return RegionalFileSource()
    raise ValueError(f"unknown source kind: {kind!r} (expected 'file' or 'wfs')")
