"""
Spanish Cadastre (SEC) lookup via WMS API.

Queries the Sede Electrónica del Catastro to retrieve the Referencia Catastral
(20-character cadastral reference) for a given coordinate pair.

API Documentation: https://www.catastro.hacienda.gob.es/ayuda/english_ovc.htm
"""

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional
from xml.etree import ElementTree

import requests

logger = logging.getLogger(__name__)

# SEC API endpoints
SEC_BASE_URL = "https://ovc.catastro.meh.es/ovcservweb/OVCSWLocalizacionRC"
SEC_COORDS_ENDPOINT = f"{SEC_BASE_URL}/OVCCoordenadas.asmx/Consulta_RCCOOR"

# Rate limiting
DEFAULT_DELAY_SECONDS = 0.5  # Be nice to the API
MAX_RETRIES = 3


@dataclass
class CadastralLookupResult:
    """Result of a cadastral lookup."""
    success: bool
    referencia_catastral: Optional[str] = None
    provincia: Optional[str] = None
    municipio: Optional[str] = None
    error: Optional[str] = None
    raw_response: Optional[str] = None


class SECCadastralClient:
    """Client for Spanish Cadastre (SEC) API lookups."""

    def __init__(
        self,
        delay_seconds: float = DEFAULT_DELAY_SECONDS,
        max_retries: int = MAX_RETRIES,
    ):
        self.delay_seconds = delay_seconds
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'SolarAcquisitionPlatform/1.0',
        })

        self.stats = {
            'requests': 0,
            'success': 0,
            'failed': 0,
            'retries': 0,
        }

    def lookup_coordinates(
        self,
        latitude: Decimal | float,
        longitude: Decimal | float,
        srs: str = "EPSG:4326",
    ) -> CadastralLookupResult:
        """
        Look up cadastral reference for given coordinates.

        Args:
            latitude: Latitude in decimal degrees
            longitude: Longitude in decimal degrees
            srs: Spatial Reference System (default: WGS84)

        Returns:
            CadastralLookupResult with referencia_catastral if found
        """
        lat = float(latitude)
        lon = float(longitude)

        # Validate coordinates are in Spain
        if not (35.0 <= lat <= 44.0 and -10.0 <= lon <= 5.0):
            return CadastralLookupResult(
                success=False,
                error=f"Coordinates ({lat}, {lon}) outside Spain bounds"
            )

        # Build request
        params = {
            'SRS': srs,
            'Coordenada_X': str(lon),  # Longitude = X
            'Coordenada_Y': str(lat),  # Latitude = Y
        }

        # Make request with retries
        for attempt in range(self.max_retries):
            try:
                self.stats['requests'] += 1

                response = self.session.get(
                    SEC_COORDS_ENDPOINT,
                    params=params,
                    timeout=30,
                )

                if response.status_code == 200:
                    result = self._parse_response(response.text)
                    if result.success:
                        self.stats['success'] += 1
                    else:
                        self.stats['failed'] += 1
                    return result
                else:
                    logger.warning(
                        f"SEC API returned status {response.status_code} "
                        f"for ({lat}, {lon})"
                    )

            except requests.exceptions.Timeout:
                logger.warning(f"Timeout on attempt {attempt + 1} for ({lat}, {lon})")
                self.stats['retries'] += 1
            except requests.exceptions.RequestException as e:
                logger.error(f"Request error on attempt {attempt + 1}: {e}")
                self.stats['retries'] += 1

            # Wait before retry
            if attempt < self.max_retries - 1:
                time.sleep(self.delay_seconds * (attempt + 1))

        self.stats['failed'] += 1
        return CadastralLookupResult(
            success=False,
            error=f"Failed after {self.max_retries} attempts"
        )

    def _parse_response(self, xml_text: str) -> CadastralLookupResult:
        """Parse SEC XML response."""
        try:
            # SEC returns XML with namespace
            root = ElementTree.fromstring(xml_text)

            # Define namespace - SEC uses default namespace
            ns = {'sec': 'http://www.catastro.meh.es/'}

            # Helper to find element with or without namespace
            def find_elem(path):
                # Try with namespace first
                elem = root.find(f'.//sec:{path}', ns)
                if elem is None:
                    # Try without namespace
                    elem = root.find(f'.//{path}')
                return elem

            # Check for errors (cuerr > 0 means error)
            cuerr = find_elem('cuerr')
            if cuerr is not None and cuerr.text and int(cuerr.text) > 0:
                # Look for error message
                err_elem = find_elem('err') or find_elem('des')
                error_msg = err_elem.text if err_elem is not None else "Unknown error"
                return CadastralLookupResult(
                    success=False,
                    error=error_msg,
                    raw_response=xml_text[:500],
                )

            # Extract Referencia Catastral from pc1 + pc2
            pc1 = find_elem('pc1')
            pc2 = find_elem('pc2')

            if pc1 is not None and pc2 is not None and pc1.text and pc2.text:
                ref_catastral = (pc1.text.strip() + pc2.text.strip()).upper()
            else:
                # Try other paths
                rc = find_elem('rc') or find_elem('refcat')
                if rc is not None and rc.text:
                    ref_catastral = rc.text.strip().upper()
                else:
                    return CadastralLookupResult(
                        success=False,
                        error="Could not extract referencia catastral from response",
                        raw_response=xml_text[:500],
                    )

            # Clean up reference
            ref_catastral = ref_catastral.replace(' ', '')

            # Log if unexpected length (should be 14 or 20 chars)
            if len(ref_catastral) not in [14, 20]:
                logger.warning(
                    f"Unexpected referencia catastral length: {len(ref_catastral)} "
                    f"(expected 14 or 20): {ref_catastral}"
                )

            # Extract location description if available
            ldt = find_elem('ldt')
            location_desc = ldt.text if ldt is not None else None

            return CadastralLookupResult(
                success=True,
                referencia_catastral=ref_catastral,
                provincia=None,
                municipio=location_desc,
            )

        except ElementTree.ParseError as e:
            logger.error(f"XML parse error: {e}")
            return CadastralLookupResult(
                success=False,
                error=f"XML parse error: {e}",
                raw_response=xml_text[:500],
            )

    def lookup_batch(
        self,
        coordinates: list[tuple[Decimal | float, Decimal | float]],
        progress_callback=None,
    ) -> list[CadastralLookupResult]:
        """
        Look up cadastral references for multiple coordinates.

        Args:
            coordinates: List of (latitude, longitude) tuples
            progress_callback: Optional callback(current, total) for progress

        Returns:
            List of CadastralLookupResult in same order as input
        """
        results = []
        total = len(coordinates)

        for i, (lat, lon) in enumerate(coordinates):
            result = self.lookup_coordinates(lat, lon)
            results.append(result)

            if progress_callback:
                progress_callback(i + 1, total)

            # Rate limiting
            if i < total - 1:
                time.sleep(self.delay_seconds)

        return results

    def get_stats(self) -> dict:
        """Return lookup statistics."""
        return self.stats.copy()


# =============================================================================
# Convenience functions
# =============================================================================

def lookup_cadastral_reference(
    latitude: Decimal | float,
    longitude: Decimal | float,
) -> Optional[str]:
    """
    Simple lookup returning just the referencia catastral or None.

    Args:
        latitude: Latitude in decimal degrees
        longitude: Longitude in decimal degrees

    Returns:
        20-character referencia catastral string, or None if not found
    """
    client = SECCadastralClient()
    result = client.lookup_coordinates(latitude, longitude)
    return result.referencia_catastral if result.success else None


# =============================================================================
# CLI for testing
# =============================================================================

if __name__ == '__main__':
    import sys

    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) < 3:
        print("Usage: python -m src.enrichment.sec_cadastral <latitude> <longitude>")
        print("Example: python -m src.enrichment.sec_cadastral 40.4168 -3.7038")
        sys.exit(1)

    lat = float(sys.argv[1])
    lon = float(sys.argv[2])

    print(f"Looking up cadastral reference for ({lat}, {lon})...")

    client = SECCadastralClient()
    result = client.lookup_coordinates(lat, lon)

    if result.success:
        print(f"Referencia Catastral: {result.referencia_catastral}")
        if result.provincia:
            print(f"Provincia: {result.provincia}")
        if result.municipio:
            print(f"Municipio: {result.municipio}")
    else:
        print(f"Lookup failed: {result.error}")
        if result.raw_response:
            print(f"Raw response: {result.raw_response}")
