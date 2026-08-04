from decimal import Decimal

import pytest
from django.urls import reverse

from catalog.enums import PricingModel
from catalog.models import Service
from users.enums import Role

LIST_URL = reverse("catalog:service-list")


@pytest.fixture
def service(db, organization):
    return Service.objects.create(
        organization=organization,
        name="Deep clean",
        pricing_model=PricingModel.PER_SQFT,
        per_sqft_rate_cents=Decimal("12.500"),
        base_price_cents=15000,
    )


@pytest.mark.django_db
class TestServiceApi:
    def test_create_stamps_the_organization(self, authed_client, organization):
        response = authed_client.post(
            LIST_URL,
            {"name": "Standard clean", "pricing_model": "flat", "base_price_cents": 12000},
            format="json",
        )

        assert response.status_code == 201
        assert Service.objects.get().organization_id == organization.id

    def test_pricing_model_coherence_is_enforced_by_the_api(self, authed_client):
        """An hourly service with no hourly rate is a 400, not a 500 later."""
        response = authed_client.post(
            LIST_URL, {"name": "Hourly", "pricing_model": "hourly"}, format="json"
        )

        assert response.status_code == 400

    def test_list_excludes_other_organizations(self, authed_client, other_organization, service):
        Service.objects.create(organization=other_organization, name="Theirs", base_price_cents=1)

        body = authed_client.get(LIST_URL).json()

        assert body["count"] == 1
        assert body["results"][0]["name"] == "Deep clean"


@pytest.mark.django_db
class TestQuoteEndpoint:
    def test_quotes_by_square_footage(self, authed_client, service):
        response = authed_client.post(
            reverse("catalog:service-quote", args=[service.id]),
            {"square_feet": 1800},
            format="json",
        )

        assert response.status_code == 200
        assert response.json()["amount_cents"] == 22500

    def test_applies_the_minimum_charge(self, authed_client, service):
        response = authed_client.post(
            reverse("catalog:service-quote", args=[service.id]),
            {"square_feet": 400},
            format="json",
        )

        assert response.json()["amount_cents"] == 15000

    def test_missing_input_is_a_400_with_a_useful_message(self, authed_client, service):
        response = authed_client.post(
            reverse("catalog:service-quote", args=[service.id]), {}, format="json"
        )

        assert response.status_code == 400
        assert "square_feet" in response.json()["detail"]

    def test_cannot_quote_another_organizations_service(self, authed_client, other_organization):
        theirs = Service.objects.create(
            organization=other_organization, name="Theirs", base_price_cents=1
        )

        response = authed_client.post(
            reverse("catalog:service-quote", args=[theirs.id]), {}, format="json"
        )

        assert response.status_code == 404


@pytest.mark.django_db
class TestCatalogPermissions:
    def test_a_cleaner_can_read_the_catalog(self, api_client, organization, make_member, service):
        """They need to know what the job involves."""
        api_client.force_login(make_member(organization, role=Role.CLEANER))

        assert api_client.get(LIST_URL).status_code == 200

    def test_a_cleaner_cannot_change_prices(self, api_client, organization, make_member):
        api_client.force_login(make_member(organization, role=Role.CLEANER))

        response = api_client.post(
            LIST_URL,
            {"name": "Cheap clean", "pricing_model": "flat", "base_price_cents": 1},
            format="json",
        )

        assert response.status_code == 403

    def test_a_dispatcher_cannot_change_prices(self, api_client, organization, make_member):
        api_client.force_login(make_member(organization, role=Role.DISPATCHER))

        response = api_client.post(
            LIST_URL,
            {"name": "Cheap clean", "pricing_model": "flat", "base_price_cents": 1},
            format="json",
        )

        assert response.status_code == 403

    def test_an_admin_can(self, api_client, organization, make_member):
        api_client.force_login(make_member(organization, role=Role.ADMIN))

        response = api_client.post(
            LIST_URL,
            {"name": "Standard", "pricing_model": "flat", "base_price_cents": 12000},
            format="json",
        )

        assert response.status_code == 201

    def test_a_customer_cannot_read_the_catalog(self, api_client, organization, make_member):
        api_client.force_login(make_member(organization, role=Role.CUSTOMER))

        assert api_client.get(LIST_URL).status_code == 403
