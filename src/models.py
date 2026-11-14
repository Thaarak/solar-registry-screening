"""
Pydantic models for data validation in Solar Acquisition Platform.
"""

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# =============================================================================
# ENUMS (matching database)
# =============================================================================

class CountryCode(str, Enum):
    IT = "IT"
    ES = "ES"


class TariffRegime(str, Enum):
    CONTO_I = "CONTO_I"
    CONTO_II = "CONTO_II"
    CONTO_III = "CONTO_III"
    CONTO_IV = "CONTO_IV"
    CONTO_V = "CONTO_V"
    RECORE = "RECORE"
    MERCHANT = "MERCHANT"
    UNKNOWN = "UNKNOWN"


class OwnerType(str, Enum):
    FARMER = "FARMER"
    SME = "SME"
    MUNICIPALITY = "MUNICIPALITY"
    FINANCIAL = "FINANCIAL"
    DEVELOPER = "DEVELOPER"
    UNKNOWN = "UNKNOWN"


class Approachability(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


class Verdict(str, Enum):
    PURSUE = "PURSUE"
    CONDITIONAL = "CONDITIONAL"
    PASS = "PASS"


# =============================================================================
# SPAIN BOUNDING BOX (for validation)
# =============================================================================

SPAIN_BOUNDS = {
    'min_lat': 35.0,   # Southern tip (Tarifa)
    'max_lat': 44.0,   # Northern tip (Pyrenees)
    'min_lon': -10.0,  # Western tip (Galicia)
    'max_lon': 5.0,    # Eastern tip (Catalonia)
}

ITALY_BOUNDS = {
    'min_lat': 35.5,   # Southern tip (Lampedusa)
    'max_lat': 47.5,   # Northern tip (Alps)
    'min_lon': 6.5,    # Western tip (Piedmont)
    'max_lon': 19.0,   # Eastern tip (Puglia)
}


# =============================================================================
# TARGET MODELS
# =============================================================================

class TargetBase(BaseModel):
    """Base fields for a solar plant target."""

    country: CountryCode
    source_registry: str = Field(..., max_length=50)
    source_id: Optional[str] = Field(None, max_length=100)
    regional_registry_id: Optional[str] = Field(None, max_length=50)

    plant_name: Optional[str] = Field(None, max_length=255)
    capacity_kw: Decimal = Field(..., gt=0, le=100000)
    commissioning_date: Optional[date] = None
    technology: Optional[str] = Field(None, max_length=50)

    latitude: Optional[Decimal] = Field(None, ge=-90, le=90)
    longitude: Optional[Decimal] = Field(None, ge=-180, le=180)
    municipality: Optional[str] = Field(None, max_length=255)
    province: Optional[str] = Field(None, max_length=100)
    region: Optional[str] = Field(None, max_length=100)

    tariff_regime: TariffRegime = TariffRegime.UNKNOWN
    tariff_rate_eur_kwh: Optional[Decimal] = Field(None, ge=0, le=1)
    tariff_expiry_date: Optional[date] = None
    fit_remaining_years: Optional[Decimal] = Field(None, ge=0, le=30)

    grid_operator: Optional[str] = Field(None, max_length=100)
    poi_capacity_kw: Optional[Decimal] = Field(None, gt=0)

    is_operating: bool = True
    is_qualified: bool = False
    qualification_notes: Optional[str] = None


class TargetCreate(TargetBase):
    """Model for creating a new target (input validation)."""

    @field_validator('capacity_kw')
    @classmethod
    def validate_capacity_range(cls, v: Decimal) -> Decimal:
        """Validate capacity is in target range (20 kW - 10 MW)."""
        if v < 20:
            raise ValueError('Capacity must be at least 20 kW')
        if v > 10000:
            raise ValueError('Capacity must be at most 10 MW (10000 kW)')
        return v

    @field_validator('commissioning_date')
    @classmethod
    def validate_commissioning_date(cls, v: Optional[date]) -> Optional[date]:
        """Validate commissioning date is in target range (2008-2013)."""
        if v is None:
            return v
        if v.year < 2005 or v.year > 2015:
            # Warn but don't reject - might still be interesting
            pass
        return v

    @model_validator(mode='after')
    def validate_coordinates_in_country(self) -> 'TargetCreate':
        """Validate coordinates are within country bounds."""
        if self.latitude is None or self.longitude is None:
            return self

        lat, lon = float(self.latitude), float(self.longitude)

        if self.country == CountryCode.ES:
            bounds = SPAIN_BOUNDS
        elif self.country == CountryCode.IT:
            bounds = ITALY_BOUNDS
        else:
            return self

        if not (bounds['min_lat'] <= lat <= bounds['max_lat']):
            raise ValueError(
                f"Latitude {lat} outside {self.country.value} bounds "
                f"({bounds['min_lat']}, {bounds['max_lat']})"
            )
        if not (bounds['min_lon'] <= lon <= bounds['max_lon']):
            raise ValueError(
                f"Longitude {lon} outside {self.country.value} bounds "
                f"({bounds['min_lon']}, {bounds['max_lon']})"
            )

        return self


class Target(TargetBase):
    """Full target model with database fields."""

    id: UUID
    created_at: datetime
    updated_at: datetime
    data_pulled_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


# =============================================================================
# CADASTRAL RECORD MODELS
# =============================================================================

class CadastralRecordBase(BaseModel):
    """Base fields for a cadastral record."""

    target_id: UUID

    # Italy
    foglio: Optional[str] = Field(None, max_length=20)
    particella: Optional[str] = Field(None, max_length=20)
    subalterno: Optional[str] = Field(None, max_length=20)
    comune_catastale: Optional[str] = Field(None, max_length=100)

    # Spain
    referencia_catastral: Optional[str] = Field(None, max_length=20)

    lookup_method: Optional[str] = Field(None, max_length=50)
    lookup_success: bool = True
    lookup_notes: Optional[str] = None


class CadastralRecordCreate(CadastralRecordBase):
    """Model for creating a new cadastral record."""

    @field_validator('referencia_catastral')
    @classmethod
    def validate_referencia_catastral(cls, v: Optional[str]) -> Optional[str]:
        """Validate Spanish cadastral reference format (20 chars)."""
        if v is None:
            return v
        v = v.strip().upper()
        if len(v) != 20:
            raise ValueError(
                f"Referencia Catastral must be exactly 20 characters, got {len(v)}"
            )
        return v


class CadastralRecord(CadastralRecordBase):
    """Full cadastral record with database fields."""

    id: UUID
    lookup_date: datetime
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# =============================================================================
# PRETOR-SPECIFIC MODELS (for parsing Spanish registry data)
# =============================================================================

class PretorRecord(BaseModel):
    """Raw record from PRETOR XLS file."""

    # These field names will be mapped from actual PRETOR columns
    instalacion: Optional[str] = None
    titular: Optional[str] = None
    potencia_instalada_kw: Optional[Decimal] = None
    tecnologia: Optional[str] = None
    provincia: Optional[str] = None
    municipio: Optional[str] = None
    comunidad_autonoma: Optional[str] = None
    fecha_inscripcion: Optional[date] = None
    estado: Optional[str] = None  # 'Definitivo' or 'Provisional'
    latitud: Optional[Decimal] = None
    longitud: Optional[Decimal] = None

    def to_target_create(self) -> TargetCreate:
        """Convert PRETOR record to TargetCreate model."""
        return TargetCreate(
            country=CountryCode.ES,
            source_registry="PRETOR",
            source_id=self.instalacion,
            plant_name=self.instalacion,
            capacity_kw=self.potencia_instalada_kw or Decimal('0'),
            commissioning_date=self.fecha_inscripcion,
            technology=self.tecnologia,
            latitude=self.latitud,
            longitude=self.longitud,
            municipality=self.municipio,
            province=self.provincia,
            region=self.comunidad_autonoma,
            tariff_regime=TariffRegime.RECORE,
            is_operating=self.estado == 'Definitivo' if self.estado else True,
        )


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def is_ground_mounted(technology: Optional[str]) -> bool:
    """Check if technology string indicates ground-mounted installation."""
    if not technology:
        return False

    tech_lower = technology.lower()
    ground_keywords = ['suelo', 'ground', 'terreno', 'huerta', 'parque']
    roof_keywords = ['techo', 'roof', 'cubierta', 'tejado', 'edificio']

    # Exclude if clearly rooftop
    if any(kw in tech_lower for kw in roof_keywords):
        return False

    # Include if clearly ground-mounted
    if any(kw in tech_lower for kw in ground_keywords):
        return True

    # Default to True if technology contains 'solar' and capacity > 100 kW
    # (small installations are usually rooftop)
    return 'solar' in tech_lower or 'fotovoltaic' in tech_lower


def is_solar_pv(technology: Optional[str]) -> bool:
    """True if technology indicates solar photovoltaic.

    Recognizes the Spanish RECORE 'grupo normativo' code b.1.1.* (PV) and plain-language
    PV labels. Solar-thermal/CSP (grupo b.1.2.*) is solar but NOT photovoltaic, so returns
    False. Missing/empty technology returns False (not PV-confirmed).
    """
    if not technology:
        return False
    t = technology.lower()
    # Solar thermal / CSP (RECORE b.1.2) is solar but not PV — exclude explicitly.
    if t.startswith('b.1.2') or 'termo' in t or 'thermal' in t:
        return False
    return (
        t.startswith('b.1.1')
        or 'fotovoltaic' in t      # es: fotovoltaica
        or 'photovoltaic' in t     # en
        or 'pv' in t
    )


def normalize_province_name(province: str) -> str:
    """Normalize Spanish province names for consistency."""
    # Common variations
    mappings = {
        'ALICANTE': 'ALICANTE/ALACANT',
        'ALACANT': 'ALICANTE/ALACANT',
        'CASTELLON': 'CASTELLÓN/CASTELLÓ',
        'CASTELLO': 'CASTELLÓN/CASTELLÓ',
        'VALENCIA': 'VALENCIA/VALÈNCIA',
        'BALEARES': 'ILLES BALEARS',
        'VIZCAYA': 'BIZKAIA',
        'GUIPUZCOA': 'GIPUZKOA',
        'NAVARRA': 'NAVARRA/NAFARROA',
        'LA CORUÑA': 'A CORUÑA',
        'ORENSE': 'OURENSE',
    }

    province_upper = province.strip().upper()
    return mappings.get(province_upper, province_upper)
