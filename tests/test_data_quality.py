"""
Data quality tests for Solar Acquisition Platform.

These tests validate data integrity and business rules for targets and cadastral records.
"""

import pytest
from decimal import Decimal
from datetime import date

from src.models import (
    TargetCreate,
    CadastralRecordCreate,
    CountryCode,
    TariffRegime,
    SPAIN_BOUNDS,
    ITALY_BOUNDS,
    is_ground_mounted,
    normalize_province_name,
)


class TestTargetValidation:
    """Tests for target data validation."""

    def test_valid_spain_target(self):
        """Test creating a valid Spanish target."""
        target = TargetCreate(
            country=CountryCode.ES,
            source_registry="PRETOR",
            source_id="TEST001",
            capacity_kw=Decimal("5000"),
            commissioning_date=date(2010, 6, 15),
            latitude=Decimal("40.4168"),
            longitude=Decimal("-3.7038"),
            province="MADRID",
            tariff_regime=TariffRegime.RECORE,
        )

        assert target.country == CountryCode.ES
        assert target.capacity_kw == Decimal("5000")
        assert target.is_qualified == False  # Default

    def test_capacity_too_small(self):
        """Test that capacity below 20 kW is rejected."""
        with pytest.raises(ValueError, match="at least 20 kW"):
            TargetCreate(
                country=CountryCode.ES,
                source_registry="PRETOR",
                source_id="TEST002",
                capacity_kw=Decimal("15"),  # Too small
            )

    def test_capacity_too_large(self):
        """Test that capacity above 10 MW is rejected."""
        with pytest.raises(ValueError, match="at most 10 MW"):
            TargetCreate(
                country=CountryCode.ES,
                source_registry="PRETOR",
                source_id="TEST003",
                capacity_kw=Decimal("15000"),  # Too large (15 MW)
            )

    def test_coordinates_in_spain(self):
        """Test that coordinates must be within Spain bounds."""
        # Valid Madrid coordinates
        target = TargetCreate(
            country=CountryCode.ES,
            source_registry="PRETOR",
            source_id="TEST004",
            capacity_kw=Decimal("1000"),
            latitude=Decimal("40.4168"),
            longitude=Decimal("-3.7038"),
        )
        assert target.latitude == Decimal("40.4168")

    def test_coordinates_outside_spain(self):
        """Test that coordinates outside Spain are rejected."""
        # Paris coordinates
        with pytest.raises(ValueError, match="outside ES bounds"):
            TargetCreate(
                country=CountryCode.ES,
                source_registry="PRETOR",
                source_id="TEST005",
                capacity_kw=Decimal("1000"),
                latitude=Decimal("48.8566"),  # Paris
                longitude=Decimal("2.3522"),
            )

    def test_valid_italy_target(self):
        """Test creating a valid Italian target."""
        target = TargetCreate(
            country=CountryCode.IT,
            source_registry="ATLAIMPIANTI",
            source_id="IT001",
            capacity_kw=Decimal("3000"),
            latitude=Decimal("41.9028"),  # Rome
            longitude=Decimal("12.4964"),
            tariff_regime=TariffRegime.CONTO_III,
        )

        assert target.country == CountryCode.IT
        assert target.tariff_regime == TariffRegime.CONTO_III

    def test_coordinates_outside_italy(self):
        """Test that coordinates outside Italy are rejected for IT targets."""
        # Madrid coordinates for Italy target
        with pytest.raises(ValueError, match="outside IT bounds"):
            TargetCreate(
                country=CountryCode.IT,
                source_registry="ATLAIMPIANTI",
                source_id="IT002",
                capacity_kw=Decimal("1000"),
                latitude=Decimal("40.4168"),  # Madrid
                longitude=Decimal("-3.7038"),
            )


class TestCadastralValidation:
    """Tests for cadastral record validation."""

    def test_valid_spain_cadastral(self):
        """Test valid Spanish cadastral reference (20 chars)."""
        from uuid import uuid4

        record = CadastralRecordCreate(
            target_id=uuid4(),
            referencia_catastral="1234567890ABCDEFGHIJ",  # Exactly 20 chars
            lookup_method="SEC_WMS",
        )

        assert record.referencia_catastral == "1234567890ABCDEFGHIJ"

    def test_invalid_spain_cadastral_length(self):
        """Test that invalid length cadastral reference is rejected."""
        from uuid import uuid4

        with pytest.raises(ValueError, match="exactly 20 characters"):
            CadastralRecordCreate(
                target_id=uuid4(),
                referencia_catastral="12345",  # Too short
            )

    def test_cadastral_uppercase_normalized(self):
        """Test that cadastral reference is uppercased."""
        from uuid import uuid4

        record = CadastralRecordCreate(
            target_id=uuid4(),
            referencia_catastral="1234567890abcdefghij",  # lowercase
        )

        assert record.referencia_catastral == "1234567890ABCDEFGHIJ"


class TestHelperFunctions:
    """Tests for helper functions."""

    def test_is_ground_mounted_positive(self):
        """Test ground-mounted detection for positive cases."""
        assert is_ground_mounted("Parque Solar") == True
        assert is_ground_mounted("Huerta Solar") == True
        assert is_ground_mounted("Planta fotovoltaica suelo") == True
        assert is_ground_mounted("Ground mounted PV") == True

    def test_is_ground_mounted_negative(self):
        """Test ground-mounted detection for rooftop cases."""
        assert is_ground_mounted("Cubierta edificio") == False
        assert is_ground_mounted("Tejado industrial") == False
        assert is_ground_mounted("Roof installation") == False

    def test_is_ground_mounted_empty(self):
        """Test ground-mounted detection for empty/None."""
        assert is_ground_mounted("") == False
        assert is_ground_mounted(None) == False

    def test_normalize_province_name(self):
        """Test Spanish province name normalization."""
        assert normalize_province_name("Alicante") == "ALICANTE/ALACANT"
        assert normalize_province_name("VALENCIA") == "VALENCIA/VALÈNCIA"
        assert normalize_province_name("La Coruña") == "A CORUÑA"
        assert normalize_province_name("Madrid") == "MADRID"  # No change


class TestBoundingBoxes:
    """Tests for geographic bounding boxes."""

    def test_spain_bounds_valid(self):
        """Test that Spain bounds are correctly defined."""
        assert SPAIN_BOUNDS['min_lat'] < SPAIN_BOUNDS['max_lat']
        assert SPAIN_BOUNDS['min_lon'] < SPAIN_BOUNDS['max_lon']

        # Madrid should be in bounds
        assert SPAIN_BOUNDS['min_lat'] <= 40.4168 <= SPAIN_BOUNDS['max_lat']
        assert SPAIN_BOUNDS['min_lon'] <= -3.7038 <= SPAIN_BOUNDS['max_lon']

    def test_italy_bounds_valid(self):
        """Test that Italy bounds are correctly defined."""
        assert ITALY_BOUNDS['min_lat'] < ITALY_BOUNDS['max_lat']
        assert ITALY_BOUNDS['min_lon'] < ITALY_BOUNDS['max_lon']

        # Rome should be in bounds
        assert ITALY_BOUNDS['min_lat'] <= 41.9028 <= ITALY_BOUNDS['max_lat']
        assert ITALY_BOUNDS['min_lon'] <= 12.4964 <= ITALY_BOUNDS['max_lon']


class TestCapacityRanges:
    """Tests for capacity range validation."""

    @pytest.mark.parametrize("capacity", [20, 100, 1000, 5000, 10000])
    def test_valid_capacities(self, capacity):
        """Test that valid capacities are accepted."""
        target = TargetCreate(
            country=CountryCode.ES,
            source_registry="TEST",
            source_id=f"CAP_{capacity}",
            capacity_kw=Decimal(str(capacity)),
        )
        assert target.capacity_kw == Decimal(str(capacity))

    @pytest.mark.parametrize("capacity", [0, 5, 10, 19])
    def test_invalid_small_capacities(self, capacity):
        """Test that capacities below 20 kW are rejected."""
        with pytest.raises(ValueError):
            TargetCreate(
                country=CountryCode.ES,
                source_registry="TEST",
                source_id=f"CAP_{capacity}",
                capacity_kw=Decimal(str(capacity)),
            )

    @pytest.mark.parametrize("capacity", [10001, 15000, 50000])
    def test_invalid_large_capacities(self, capacity):
        """Test that capacities above 10 MW are rejected."""
        with pytest.raises(ValueError):
            TargetCreate(
                country=CountryCode.ES,
                source_registry="TEST",
                source_id=f"CAP_{capacity}",
                capacity_kw=Decimal(str(capacity)),
            )
