"""
Grid-congestion / curtailment enrichment (Bucket-1 coordinate-free).

Spanish solar curtailment and POI firmness are driven by *where on the grid* a
plant sits — and that is well-approximated by province / grid zone, with no need
for per-plant coordinates. REE (Red Eléctrica de España) publishes
capacity-access maps ("mapas de capacidad de acceso") and saturated-node lists
that classify the transmission grid by congestion. This module distils that into
a curated province -> congestion classification so every qualified ES target can
be scored offline, with an optional live refresh from the ESIOS API.

This mirrors the `regional_registry.py` source posture:
- a curated table guarantees 100% offline coverage,
- an optional network refresh (ESIOS, token-gated) is cached to disk and falls
  back gracefully to the curated table when unavailable,
- `get_stats()` exposes a small counters dict for the CLI report.

The classification is deliberately coarse (none / moderate / high) — it is a
screening/triage signal that contextualises the curtailment and export-POI
criteria, not a per-node engineering verdict.
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)


# =============================================================================
# Curated province -> congestion classification
# =============================================================================
#
# Seeded from REE's published capacity-access / saturated-node maps. Each entry:
#   curtailment_risk : 'none' | 'moderate' | 'high'
#   grid_zone        : a coarse REE grid-zone label (province-level proxy)
#
# Rationale for the risk banding (sub-10 MW solar lens):
#   'high'     — provinces in the southern/western PV belt where transmission
#                access is saturated and curtailment events are recurrent
#                (heavy RES penetration: Andalucía, Extremadura, Castilla-La
#                Mancha PV core, Aragón).
#   'moderate' — provinces with meaningful RES build-out but residual headroom.
#   'none'     — provinces with low PV penetration / spare access (northern
#                Spain, islands handled separately, low-irradiation areas).
#
# Province keys are upper-cased and accent-free to match the normalization used
# across the pipeline (see `normalize_province` below).

PROVINCE_CONGESTION: dict[str, dict] = {
    # --- Andalucía (southern PV belt — saturated) ---
    'ALMERIA': {'curtailment_risk': 'high', 'grid_zone': 'SUR'},
    'CADIZ': {'curtailment_risk': 'high', 'grid_zone': 'SUR'},
    'CORDOBA': {'curtailment_risk': 'high', 'grid_zone': 'SUR'},
    'GRANADA': {'curtailment_risk': 'high', 'grid_zone': 'SUR'},
    'HUELVA': {'curtailment_risk': 'high', 'grid_zone': 'SUR'},
    'JAEN': {'curtailment_risk': 'high', 'grid_zone': 'SUR'},
    'MALAGA': {'curtailment_risk': 'moderate', 'grid_zone': 'SUR'},
    'SEVILLA': {'curtailment_risk': 'high', 'grid_zone': 'SUR'},

    # --- Extremadura (PV core — saturated) ---
    'BADAJOZ': {'curtailment_risk': 'high', 'grid_zone': 'SUROESTE'},
    'CACERES': {'curtailment_risk': 'high', 'grid_zone': 'SUROESTE'},

    # --- Castilla-La Mancha (PV core — saturated) ---
    'ALBACETE': {'curtailment_risk': 'high', 'grid_zone': 'CENTRO'},
    'CIUDAD REAL': {'curtailment_risk': 'high', 'grid_zone': 'CENTRO'},
    'CUENCA': {'curtailment_risk': 'moderate', 'grid_zone': 'CENTRO'},
    'GUADALAJARA': {'curtailment_risk': 'moderate', 'grid_zone': 'CENTRO'},
    'TOLEDO': {'curtailment_risk': 'high', 'grid_zone': 'CENTRO'},

    # --- Aragón (heavy wind+solar build-out — saturated) ---
    'HUESCA': {'curtailment_risk': 'high', 'grid_zone': 'NORDESTE'},
    'TERUEL': {'curtailment_risk': 'high', 'grid_zone': 'NORDESTE'},
    'ZARAGOZA': {'curtailment_risk': 'high', 'grid_zone': 'NORDESTE'},

    # --- Castilla y León (mixed — moderate, with hotspots) ---
    'AVILA': {'curtailment_risk': 'moderate', 'grid_zone': 'NOROESTE'},
    'BURGOS': {'curtailment_risk': 'moderate', 'grid_zone': 'NOROESTE'},
    'LEON': {'curtailment_risk': 'moderate', 'grid_zone': 'NOROESTE'},
    'PALENCIA': {'curtailment_risk': 'moderate', 'grid_zone': 'NOROESTE'},
    'SALAMANCA': {'curtailment_risk': 'high', 'grid_zone': 'NOROESTE'},
    'SEGOVIA': {'curtailment_risk': 'moderate', 'grid_zone': 'NOROESTE'},
    'SORIA': {'curtailment_risk': 'moderate', 'grid_zone': 'NOROESTE'},
    'VALLADOLID': {'curtailment_risk': 'moderate', 'grid_zone': 'NOROESTE'},
    'ZAMORA': {'curtailment_risk': 'moderate', 'grid_zone': 'NOROESTE'},

    # --- Madrid (demand centre — moderate access) ---
    'MADRID': {'curtailment_risk': 'moderate', 'grid_zone': 'CENTRO'},

    # --- Murcia / Valencia (Levante PV — moderate/high) ---
    'MURCIA': {'curtailment_risk': 'high', 'grid_zone': 'LEVANTE'},
    'ALICANTE': {'curtailment_risk': 'moderate', 'grid_zone': 'LEVANTE'},
    'CASTELLON': {'curtailment_risk': 'moderate', 'grid_zone': 'LEVANTE'},
    'VALENCIA': {'curtailment_risk': 'moderate', 'grid_zone': 'LEVANTE'},

    # --- Cataluña (mixed — moderate) ---
    'BARCELONA': {'curtailment_risk': 'none', 'grid_zone': 'NORDESTE'},
    'GIRONA': {'curtailment_risk': 'none', 'grid_zone': 'NORDESTE'},
    'LLEIDA': {'curtailment_risk': 'moderate', 'grid_zone': 'NORDESTE'},
    'TARRAGONA': {'curtailment_risk': 'moderate', 'grid_zone': 'NORDESTE'},

    # --- Northern Spain (low PV penetration — spare access) ---
    'A CORUNA': {'curtailment_risk': 'none', 'grid_zone': 'NOROESTE'},
    'LUGO': {'curtailment_risk': 'none', 'grid_zone': 'NOROESTE'},
    'OURENSE': {'curtailment_risk': 'moderate', 'grid_zone': 'NOROESTE'},
    'PONTEVEDRA': {'curtailment_risk': 'none', 'grid_zone': 'NOROESTE'},
    'ASTURIAS': {'curtailment_risk': 'none', 'grid_zone': 'NORTE'},
    'CANTABRIA': {'curtailment_risk': 'none', 'grid_zone': 'NORTE'},
    'BIZKAIA': {'curtailment_risk': 'none', 'grid_zone': 'NORTE'},
    'GIPUZKOA': {'curtailment_risk': 'none', 'grid_zone': 'NORTE'},
    'ARABA': {'curtailment_risk': 'none', 'grid_zone': 'NORTE'},
    'NAVARRA': {'curtailment_risk': 'moderate', 'grid_zone': 'NORTE'},
    'LA RIOJA': {'curtailment_risk': 'moderate', 'grid_zone': 'NORTE'},

    # --- Islands (isolated systems — own constraints) ---
    'BALEARES': {'curtailment_risk': 'moderate', 'grid_zone': 'BALEARES'},
    'LAS PALMAS': {'curtailment_risk': 'moderate', 'grid_zone': 'CANARIAS'},
    'SANTA CRUZ DE TENERIFE': {'curtailment_risk': 'moderate', 'grid_zone': 'CANARIAS'},
}

# Default when a province is unrecognised: unknown congestion, generic zone.
DEFAULT_CLASSIFICATION = {'curtailment_risk': 'moderate', 'grid_zone': 'UNKNOWN'}

# Province-name aliases (accent-free, upper-cased) folded onto the canonical keys.
PROVINCE_ALIASES: dict[str, str] = {
    'CORUNA': 'A CORUNA',
    'LA CORUNA': 'A CORUNA',
    'VIZCAYA': 'BIZKAIA',
    'GUIPUZCOA': 'GIPUZKOA',
    'ALAVA': 'ARABA',
    'ILLES BALEARS': 'BALEARES',
    'ISLAS BALEARES': 'BALEARES',
    'STA CRUZ DE TENERIFE': 'SANTA CRUZ DE TENERIFE',
    'TENERIFE': 'SANTA CRUZ DE TENERIFE',
    'CASTELLO': 'CASTELLON',
    'GERONA': 'GIRONA',
    'LERIDA': 'LLEIDA',
}

# ESIOS API base (indicator endpoint); token-gated, cached on disk.
ESIOS_BASE_URL = 'https://api.esios.ree.es'
CACHE_PATH = Path(__file__).parent.parent.parent / 'data' / 'cache' / 'grid_congestion.json'


# =============================================================================
# Normalization
# =============================================================================

def _strip_accents(text: str) -> str:
    """Fold common Spanish accents so province keys compare accent-free."""
    table = str.maketrans('ÁÉÍÓÚÀÈÌÒÙÄËÏÖÜÑ', 'AEIOUAEIOUAEIOUN')
    return text.translate(table)


def normalize_province(province: Optional[str]) -> Optional[str]:
    """Normalize a province name for table lookup (upper, accent-free, aliased)."""
    if not province:
        return None
    key = _strip_accents(province.strip().upper())
    key = ' '.join(key.split())
    return PROVINCE_ALIASES.get(key, key) or None


# =============================================================================
# Source
# =============================================================================

class GridCongestionSource:
    """
    Province-level grid-congestion classifier.

    Resolves each province to `{curtailment_risk, grid_zone}` from a curated REE
    table (100% offline coverage). An optional `refresh()` pulls a live snapshot
    from the ESIOS API (token from env `ESIOS_TOKEN`), caches it to disk, and
    overlays it on the curated baseline; on any failure it logs a clear reason
    and keeps the curated table (graceful degradation, like the WFS source).
    """

    name = 'REE_GRID'

    def __init__(self, timeout: int = 30, cache_path: Optional[Path] = None):
        self.timeout = timeout
        self.cache_path = Path(cache_path) if cache_path else CACHE_PATH
        # Working table starts as the curated baseline; refresh() may overlay.
        self.table: dict[str, dict] = dict(PROVINCE_CONGESTION)
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'SolarAcquisitionPlatform/1.0 (research project)',
        })
        self.stats = {
            'classified': 0,
            'curated_hits': 0,
            'unknown_province': 0,
            'live_refreshed': 0,
            'live_errors': 0,
        }

    def refresh(self) -> bool:
        """
        Best-effort live refresh from ESIOS. Returns True if a live snapshot was
        applied, False if it fell back to the curated table.

        Token-gated (env `ESIOS_TOKEN`). On no token / network error / bad
        payload it logs and returns False, leaving the curated table intact.
        """
        # Use a disk cache first, so repeat runs don't re-hit the API.
        cached = self._load_cache()
        if cached:
            self._overlay(cached)
            logger.info(f"[{self.name}] using cached live snapshot ({len(cached)} provinces)")
            self.stats['live_refreshed'] += len(cached)
            return True

        token = os.environ.get('ESIOS_TOKEN')
        if not token:
            logger.info(
                f"[{self.name}] no ESIOS_TOKEN set — using curated congestion table "
                f"({len(self.table)} provinces, offline)"
            )
            return False

        try:
            snapshot = self._fetch_esios(token)
        except (requests.RequestException, ValueError, KeyError) as e:
            self.stats['live_errors'] += 1
            logger.warning(f"[{self.name}] ESIOS refresh failed, keeping curated table: {e}")
            return False

        if not snapshot:
            logger.info(f"[{self.name}] ESIOS returned no usable data, keeping curated table")
            return False

        self._overlay(snapshot)
        self._save_cache(snapshot)
        self.stats['live_refreshed'] += len(snapshot)
        logger.info(f"[{self.name}] applied live ESIOS snapshot ({len(snapshot)} provinces)")
        return True

    def _fetch_esios(self, token: str) -> dict[str, dict]:
        """
        Pull a curtailment-related indicator from ESIOS and reduce it to a
        province -> classification overlay.

        ESIOS exposes national/zonal indicators rather than per-province
        congestion directly, so this is a thin, defensive adapter: it requests
        the indicators listing and, absent a clean province mapping, returns {}
        (the curated table then stands). The wiring exists so a confirmed
        indicator id can be slotted in without code change elsewhere.
        """
        headers = {
            'Accept': 'application/json; application/vnd.esios-api-v1+json',
            'Content-Type': 'application/json',
            'x-api-key': token,
        }
        resp = self.session.get(
            f"{ESIOS_BASE_URL}/indicators",
            headers=headers,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        # No authoritative province-level congestion indicator is confirmed yet;
        # validate the response shape and return an empty overlay (curated stands).
        if not isinstance(data, dict) or 'indicators' not in data:
            raise ValueError("unexpected ESIOS payload shape")
        return {}

    def _overlay(self, snapshot: dict[str, dict]) -> None:
        """Overlay a live snapshot on the curated baseline (province-keyed)."""
        for province, cls in snapshot.items():
            key = normalize_province(province)
            if key and isinstance(cls, dict) and 'curtailment_risk' in cls:
                base = self.table.get(key, dict(DEFAULT_CLASSIFICATION))
                self.table[key] = {**base, **cls}

    def _load_cache(self) -> Optional[dict]:
        if not self.cache_path.exists():
            return None
        try:
            return json.loads(self.cache_path.read_text(encoding='utf-8'))
        except (ValueError, OSError) as e:
            logger.warning(f"[{self.name}] cache read failed: {e}")
            return None

    def _save_cache(self, snapshot: dict) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(snapshot, indent=2), encoding='utf-8')
        except OSError as e:
            logger.warning(f"[{self.name}] cache write failed: {e}")

    def classify(self, province: Optional[str]) -> dict:
        """
        Return `{curtailment_risk, grid_zone}` for a province.

        Falls back to a neutral default for unrecognised provinces so the caller
        always gets a usable classification.
        """
        self.stats['classified'] += 1
        key = normalize_province(province)
        if key and key in self.table:
            self.stats['curated_hits'] += 1
            return dict(self.table[key])
        self.stats['unknown_province'] += 1
        return dict(DEFAULT_CLASSIFICATION)

    def get_stats(self) -> dict:
        return self.stats.copy()


# =============================================================================
# CLI for quick inspection
# =============================================================================

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)

    src = GridCongestionSource()
    src.refresh()

    print(f"Curated provinces: {len(PROVINCE_CONGESTION)}")
    for prov in ['Sevilla', 'Badajoz', 'Asturias', 'Zaragoza', 'Nonexistent']:
        print(f"  {prov:>12} -> {src.classify(prov)}")
    print(f"\nStats: {src.get_stats()}")
