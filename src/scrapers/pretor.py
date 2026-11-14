"""
PRETOR (Spain MITECO) registry parser.

Parses XLS/XLSX files from the Spanish Ministry for Ecological Transition
containing registered electricity production installations.

Data source: https://www.miteco.gob.es/es/energia/energia-electrica/electricidad/energias-renovables/registro-administrativo.html
"""

import logging
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Generator, Optional

import pandas as pd
from pydantic import ValidationError

from src.models import (
    TargetCreate,
    CountryCode,
    TariffRegime,
    is_solar_pv,
    normalize_province_name,
)

logger = logging.getLogger(__name__)

# Expected column mappings (PRETOR format varies by region)
# These are common column names - adjust based on actual file
COLUMN_MAPPINGS = {
    # Installation ID
    'instalacion': ['clave registro', 'instalacion', 'instalación', 'nombre', 'denominacion', 'id'],
    # Regional registry ID (Número de Registro Autonómico Definitivo) - join key into regional registries
    'registro_autonomico': [
        'número de registro autonómico definitivo', 'numero de registro autonomico definitivo',
        'registro autonómico definitivo', 'registro autonomico definitivo',
        'número de registro autonómico', 'numero de registro autonomico',
        'registro autonómico', 'registro autonomico',
    ],
    # Plant name
    'nombre': ['nombre de instalación', 'nombre de instalacion', 'nombre instalación', 'nombre', 'instalacion'],
    # Owner/holder
    'titular': ['titular', 'propietario', 'empresa', 'razón social'],
    # Capacity
    'potencia_kw': ['potencia instalada kw', 'potencia instalada', 'potencia (kw)', 'potencia_kw', 'p. instalada', 'potencia'],
    # Technology / Regulatory Group (b.1.1 = solar PV)
    'tecnologia': ['grupo normativo', 'tecnologia', 'tecnología', 'tipo', 'fuente', 'recurso'],
    # Location
    'provincia': ['provincia de la instalación', 'provincia de la instalacion', 'provincia', 'prov'],
    'municipio': ['municipio de la instalación', 'municipio de la instalacion', 'municipio', 'localidad', 'poblacion'],
    'comunidad': ['comunidad autónoma', 'ccaa', 'comunidad', 'ca'],
    # Dates
    'fecha_inscripcion': ['fecha de inscripción definitiva', 'fecha de inscripcion definitiva', 'fecha inscripción', 'fecha de inscripción', 'fecha', 'f. inscripcion'],
    # Status
    'estado': ['tipo de inscripción', 'tipo de inscripcion', 'tipo inscripción', 'tipo inscripcion', 'estado', 'situación'],
    # Coordinates (if available)
    'latitud': ['latitud', 'lat', 'y'],
    'longitud': ['longitud', 'lon', 'long', 'x'],
}

def find_column(df: pd.DataFrame, possible_names: list[str]) -> Optional[str]:
    """Find a column by checking possible name variations."""
    df_columns_lower = {str(col).lower().strip(): col for col in df.columns}

    for name in possible_names:
        name_lower = name.lower().strip()
        if name_lower in df_columns_lower:
            return df_columns_lower[name_lower]

    return None


def parse_decimal(value) -> Optional[Decimal]:
    """Safely parse a value to Decimal."""
    if pd.isna(value):
        return None
    try:
        # Handle string with comma as decimal separator
        if isinstance(value, str):
            value = value.replace(',', '.').strip()
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def parse_regional_id(value) -> Optional[str]:
    """Clean a regional registry id (Número de Registro Autonómico Definitivo)."""
    if pd.isna(value):
        return None
    cleaned = str(value).strip()
    if not cleaned or cleaned.upper() in {'NAN', 'N/D', 'N/A', '-'}:
        return None
    return cleaned[:50]


def parse_date(value) -> Optional[date]:
    """Safely parse a value to date."""
    if pd.isna(value):
        return None
    try:
        if isinstance(value, pd.Timestamp):
            return value.date()
        if isinstance(value, date):
            return value
        # Try common formats
        for fmt in ['%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%Y']:
            try:
                return pd.to_datetime(value, format=fmt).date()
            except ValueError:
                continue
        return pd.to_datetime(value).date()
    except Exception:
        return None


class PretorParser:
    """Parser for PRETOR registry exports."""

    def __init__(
        self,
        min_capacity_kw: Decimal = Decimal('20'),
        max_capacity_kw: Decimal = Decimal('10000'),
        min_year: int = 2008,
        max_year: int = 2013,
        only_ground_mounted: bool = True,
        only_definitive: bool = True,
    ):
        self.min_capacity_kw = min_capacity_kw
        self.max_capacity_kw = max_capacity_kw
        self.min_year = min_year
        self.max_year = max_year
        self.only_ground_mounted = only_ground_mounted
        self.only_definitive = only_definitive

        self.stats = {
            'total_rows': 0,
            'parsed': 0,
            'filtered_capacity': 0,
            'filtered_date': 0,
            'filtered_technology': 0,
            'filtered_status': 0,
            'validation_errors': 0,
        }

    def parse_file(self, file_path: Path) -> Generator[TargetCreate, None, None]:
        """Parse a PRETOR-style CSV or spreadsheet and yield qualified targets."""
        logger.info(f"Parsing PRETOR file: {file_path}")

        # Read file - detect format first
        try:
            # Check if file is actually HTML (common with PRETOR exports)
            with open(file_path, 'rb') as f:
                header = f.read(100)

            if header.startswith(b'<table') or b'<table' in header:
                # HTML table disguised as XLS
                logger.info("Detected HTML format, using read_html")
                dfs = pd.read_html(str(file_path), encoding='iso-8859-1', header=0)
                if not dfs:
                    raise ValueError("No tables found in HTML file")
                df = dfs[0]
                # Clean column names - convert any non-string columns to string
                df.columns = [str(col).strip() for col in df.columns]
            elif file_path.suffix.lower() == '.csv':
                df = pd.read_csv(file_path)
            elif file_path.suffix.lower() == '.xls':
                df = pd.read_excel(file_path, engine='xlrd')
            else:
                df = pd.read_excel(file_path, engine='openpyxl')
        except Exception as e:
            logger.error(f"Failed to read file: {e}")
            raise

        self.stats['total_rows'] = len(df)
        logger.info(f"Read {len(df)} rows from {file_path.name}")

        # Map columns
        col_map = {}
        for field, possible_names in COLUMN_MAPPINGS.items():
            col = find_column(df, possible_names)
            if col:
                col_map[field] = col
                logger.debug(f"Mapped '{field}' to column '{col}'")
            else:
                logger.debug(f"Could not find optional column for '{field}'")

        required = {"potencia_kw", "tecnologia"}
        if missing := required - col_map.keys():
            raise ValueError(f"Missing required registry columns: {', '.join(sorted(missing))}")

        # Process rows
        for idx, row in df.iterrows():
            target = self._process_row(row, col_map, idx)
            if target:
                yield target

        # Log stats
        logger.info(f"Parsing complete. Stats: {self.stats}")

    def _process_row(
        self,
        row: pd.Series,
        col_map: dict[str, str],
        idx: int
    ) -> Optional[TargetCreate]:
        """Process a single row and return TargetCreate if qualified."""

        # Extract values
        capacity = parse_decimal(row.get(col_map.get('potencia_kw')))
        fecha = parse_date(row.get(col_map.get('fecha_inscripcion')))
        grupo_normativo = str(row.get(col_map.get('tecnologia'), '')).strip().lower()
        estado = str(row.get(col_map.get('estado'), '')).strip().lower()

        # Registry technology identifies PV but does not always specify the mounting type.
        name = str(row.get(col_map.get('nombre'), '')).strip()
        roof_terms = ('techo', 'rooftop', 'cubierta', 'tejado')
        explicit_rooftop = any(term in f"{grupo_normativo} {name.lower()}" for term in roof_terms)
        if not is_solar_pv(grupo_normativo) or (self.only_ground_mounted and explicit_rooftop):
            self.stats['filtered_technology'] += 1
            return None

        # Filter: capacity range
        if capacity is None or capacity < self.min_capacity_kw or capacity > self.max_capacity_kw:
            self.stats['filtered_capacity'] += 1
            return None

        # Filter: commissioning date
        if fecha and (fecha.year < self.min_year or fecha.year > self.max_year):
            self.stats['filtered_date'] += 1
            return None

        # Filter: definitive status only
        if self.only_definitive and estado not in {'', 'nan'} and 'definitiv' not in estado:
            self.stats['filtered_status'] += 1
            return None

        # Build target
        try:
            provincia = str(row.get(col_map.get('provincia'), '')).strip()
            if provincia:
                provincia = normalize_province_name(provincia)

            # Get source_id from clave registro, plant_name from nombre
            source_id = str(row.get(col_map.get('instalacion'), f'PRETOR_{idx}')).strip()[:100]
            plant_name = name[:255]
            if not plant_name:
                plant_name = source_id  # Fallback to source_id if no name

            regional_registry_id = parse_regional_id(row.get(col_map.get('registro_autonomico')))

            target = TargetCreate(
                country=CountryCode.ES,
                source_registry="PRETOR",
                source_id=source_id,
                regional_registry_id=regional_registry_id,
                plant_name=plant_name or None,
                capacity_kw=capacity,
                commissioning_date=fecha,
                technology=grupo_normativo[:50] if grupo_normativo else "b.1.1",
                latitude=parse_decimal(row.get(col_map.get('latitud'))),
                longitude=parse_decimal(row.get(col_map.get('longitud'))),
                municipality=str(row.get(col_map.get('municipio'), '')).strip()[:255] or None,
                province=provincia[:100] if provincia else None,
                region=str(row.get(col_map.get('comunidad'), '')).strip()[:100] or None,
                tariff_regime=TariffRegime.RECORE,
                is_operating=True,
                is_qualified=True,  # Passed all filters
            )

            self.stats['parsed'] += 1
            return target

        except ValidationError as e:
            logger.warning(f"Validation error at row {idx}: {e}")
            self.stats['validation_errors'] += 1
            return None

    def get_stats(self) -> dict:
        """Return parsing statistics."""
        return self.stats.copy()


def parse_pretor_file(
    file_path: str | Path,
    **filter_kwargs
) -> list[TargetCreate]:
    """
    Convenience function to parse a PRETOR file and return list of targets.

    Args:
        file_path: Path to PRETOR XLS/XLSX file
        **filter_kwargs: Passed to PretorParser (min_capacity_kw, max_capacity_kw, etc.)

    Returns:
        List of TargetCreate objects
    """
    parser = PretorParser(**filter_kwargs)
    targets = list(parser.parse_file(Path(file_path)))
    return targets


# =============================================================================
# CLI for testing
# =============================================================================

if __name__ == '__main__':
    import sys

    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) < 2:
        print("Usage: python -m src.scrapers.pretor <file.xls>")
        sys.exit(1)

    file_path = Path(sys.argv[1])
    if not file_path.exists():
        print(f"File not found: {file_path}")
        sys.exit(1)

    parser = PretorParser()
    targets = list(parser.parse_file(file_path))

    print(f"\nFound {len(targets)} qualified targets")
    print(f"\nStats: {parser.get_stats()}")

    if targets:
        print("\nSample (first 5):")
        for t in targets[:5]:
            print(f"  - {t.plant_name or t.source_id}: {t.capacity_kw} kW, {t.province}")
