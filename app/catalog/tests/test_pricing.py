from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from catalog.enums import PricingModel
from catalog.models import Service


@pytest.fixture
def make_service(db, organization):
    def _make(**kwargs):
        return Service.objects.create(
            organization=organization,
            name=kwargs.pop("name", "Standard clean"),
            **kwargs,
        )

    return _make


@pytest.mark.django_db
class TestFlatPricing:
    def test_returns_the_base_price(self, make_service):
        service = make_service(pricing_model=PricingModel.FLAT, base_price_cents=15000)

        assert service.quote_cents() == 15000

    def test_ignores_irrelevant_inputs(self, make_service):
        service = make_service(pricing_model=PricingModel.FLAT, base_price_cents=15000)

        assert service.quote_cents(square_feet=9000, hours=40) == 15000


@pytest.mark.django_db
class TestHourlyPricing:
    def test_multiplies_rate_by_hours(self, make_service):
        service = make_service(
            pricing_model=PricingModel.HOURLY, hourly_rate_cents=Decimal("4500.00")
        )

        assert service.quote_cents(hours=3) == 13500

    def test_handles_fractional_hours(self, make_service):
        service = make_service(
            pricing_model=PricingModel.HOURLY, hourly_rate_cents=Decimal("4500.00")
        )

        assert service.quote_cents(hours=Decimal("2.5")) == 11250

    def test_base_price_acts_as_a_minimum(self, make_service):
        """A 30-minute callout should not price below what it costs to show up."""
        service = make_service(
            pricing_model=PricingModel.HOURLY,
            hourly_rate_cents=Decimal("4500.00"),
            base_price_cents=8000,
        )

        assert service.quote_cents(hours=Decimal("0.5")) == 8000

    def test_missing_hours_raises_rather_than_quoting_zero(self, make_service):
        service = make_service(
            pricing_model=PricingModel.HOURLY, hourly_rate_cents=Decimal("4500.00")
        )

        with pytest.raises(ValueError, match="hours"):
            service.quote_cents(square_feet=1200)


@pytest.mark.django_db
class TestPerSqftPricing:
    def test_multiplies_rate_by_area(self, make_service):
        service = make_service(
            pricing_model=PricingModel.PER_SQFT, per_sqft_rate_cents=Decimal("12.000")
        )

        assert service.quote_cents(square_feet=1800) == 21600

    def test_fractional_rates_do_not_lose_money(self, make_service):
        """
        12.5 cents/sqft over 1800 sqft is $225.00. Rounding the *rate* to 12
        would quote $216 and quietly lose $9 on every job -- which is why the
        rate is Decimal and only the total is rounded.
        """
        service = make_service(
            pricing_model=PricingModel.PER_SQFT, per_sqft_rate_cents=Decimal("12.500")
        )

        assert service.quote_cents(square_feet=1800) == 22500

    def test_rounds_half_up_at_the_end(self, make_service):
        service = make_service(
            pricing_model=PricingModel.PER_SQFT, per_sqft_rate_cents=Decimal("12.345")
        )

        # 12.345 * 1001 = 12357.345 -> 12357
        assert service.quote_cents(square_feet=1001) == 12357

    def test_base_price_acts_as_a_minimum(self, make_service):
        service = make_service(
            pricing_model=PricingModel.PER_SQFT,
            per_sqft_rate_cents=Decimal("12.000"),
            base_price_cents=20000,
        )

        assert service.quote_cents(square_feet=400) == 20000

    def test_missing_area_raises(self, make_service):
        service = make_service(
            pricing_model=PricingModel.PER_SQFT, per_sqft_rate_cents=Decimal("12.000")
        )

        with pytest.raises(ValueError, match="square_feet"):
            service.quote_cents(hours=3)


@pytest.mark.django_db
class TestServiceValidation:
    def test_hourly_service_needs_a_rate(self, organization):
        service = Service(
            organization=organization, name="Hourly", pricing_model=PricingModel.HOURLY
        )

        with pytest.raises(ValidationError, match="hourly_rate_cents"):
            service.clean()

    def test_per_sqft_service_needs_a_rate(self, organization):
        service = Service(
            organization=organization, name="Sqft", pricing_model=PricingModel.PER_SQFT
        )

        with pytest.raises(ValidationError, match="per_sqft_rate_cents"):
            service.clean()

    def test_flat_service_needs_a_price(self, organization):
        service = Service(organization=organization, name="Flat", pricing_model=PricingModel.FLAT)

        with pytest.raises(ValidationError, match="base_price_cents"):
            service.clean()

    def test_a_valid_service_passes(self, organization):
        service = Service(
            organization=organization,
            name="Flat",
            pricing_model=PricingModel.FLAT,
            base_price_cents=12000,
        )

        service.clean()  # does not raise

    def test_service_names_are_unique_per_organization(self, organization, other_organization):
        from django.db.utils import IntegrityError

        Service.objects.create(organization=organization, name="Deep clean", base_price_cents=1)
        # Same name in a different organization is fine.
        Service.objects.create(
            organization=other_organization, name="Deep clean", base_price_cents=1
        )

        with pytest.raises(IntegrityError):
            Service.objects.create(organization=organization, name="Deep clean", base_price_cents=1)
