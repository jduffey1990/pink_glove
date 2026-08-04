import pytest
from django.urls import reverse

from customers.models import Customer, ServiceLocation
from users.enums import Role

LIST_URL = reverse("customers:customer-list")


@pytest.fixture
def make_customer(db):
    def _make(organization, **kwargs):
        kwargs.setdefault("first_name", "Dana")
        kwargs.setdefault("last_name", "Wu")
        return Customer.objects.create(organization=organization, **kwargs)

    return _make


@pytest.mark.django_db
class TestCustomerApi:
    def test_create_stamps_the_callers_organization(self, authed_client, organization):
        response = authed_client.post(
            LIST_URL, {"first_name": "Dana", "last_name": "Wu"}, format="json"
        )

        assert response.status_code == 201
        assert Customer.objects.get().organization_id == organization.id

    def test_a_forged_organization_in_the_payload_is_ignored(
        self, authed_client, organization, other_organization
    ):
        response = authed_client.post(
            LIST_URL,
            {
                "first_name": "Dana",
                "last_name": "Wu",
                "organization": str(other_organization.id),
            },
            format="json",
        )

        assert response.status_code == 201
        assert Customer.objects.get().organization_id == organization.id

    def test_a_customer_needs_some_kind_of_name(self, authed_client):
        response = authed_client.post(LIST_URL, {"email": "x@example.com"}, format="json")

        assert response.status_code == 400

    def test_company_name_alone_is_enough(self, authed_client):
        response = authed_client.post(LIST_URL, {"company_name": "Acme LLC"}, format="json")

        assert response.status_code == 201

    def test_search_matches_name_and_email(self, authed_client, organization, make_customer):
        make_customer(organization, first_name="Dana", last_name="Wu")
        make_customer(organization, first_name="Sam", last_name="Ortiz")

        response = authed_client.get(LIST_URL, {"search": "ortiz"})

        assert response.json()["count"] == 1

    def test_status_filter(self, authed_client, organization, make_customer):
        make_customer(organization, status="active")
        make_customer(organization, status="lead")

        assert authed_client.get(LIST_URL, {"status": "active"}).json()["count"] == 1


@pytest.mark.django_db
class TestCustomerTenantIsolation:
    def test_list_excludes_other_organizations(
        self, authed_client, organization, other_organization, make_customer
    ):
        make_customer(organization, first_name="Mine")
        make_customer(other_organization, first_name="Theirs")

        body = authed_client.get(LIST_URL).json()

        assert body["count"] == 1
        assert body["results"][0]["first_name"] == "Mine"

    def test_detail_of_another_organizations_customer_is_404(
        self, authed_client, other_organization, make_customer
    ):
        theirs = make_customer(other_organization)

        response = authed_client.get(reverse("customers:customer-detail", args=[theirs.id]))

        assert response.status_code == 404

    def test_cannot_update_another_organizations_customer(
        self, authed_client, other_organization, make_customer
    ):
        theirs = make_customer(other_organization)

        response = authed_client.patch(
            reverse("customers:customer-detail", args=[theirs.id]),
            {"first_name": "Hijacked"},
            format="json",
        )

        assert response.status_code == 404
        theirs.refresh_from_db()
        assert theirs.first_name == "Dana"

    def test_cannot_delete_another_organizations_customer(
        self, authed_client, other_organization, make_customer
    ):
        theirs = make_customer(other_organization)

        response = authed_client.delete(reverse("customers:customer-detail", args=[theirs.id]))

        assert response.status_code == 404
        assert Customer.objects.filter(pk=theirs.pk).exists()


@pytest.mark.django_db
class TestServiceLocationApi:
    def test_cannot_attach_a_location_to_another_organizations_customer(
        self, authed_client, other_organization, make_customer
    ):
        """
        The cross-organization FK guard. Queryset scoping hides their customer
        from reads, but nothing stops a caller pasting the id into a write --
        except TenantModel.save().
        """
        theirs = make_customer(other_organization)

        response = authed_client.post(
            reverse("customers:location-list"),
            {
                "customer": str(theirs.id),
                "line1": "1 Elm St",
                "city": "Denver",
                "state": "CO",
                "postal_code": "80202",
            },
            format="json",
        )

        # 400, not 500: the guard raises Django's ValidationError, which
        # app.exceptions.exception_handler renders as a client error.
        assert response.status_code == 400
        assert "customer" in response.json()
        assert not ServiceLocation.objects.filter(customer=theirs).exists()

    def test_nested_location_summary_hides_access_codes(
        self, authed_client, organization, make_customer
    ):
        """A customer list must not spray gate codes across the wire."""
        customer = make_customer(organization)
        ServiceLocation.objects.create(
            organization=organization,
            customer=customer,
            line1="1 Elm St",
            city="Denver",
            state="CO",
            postal_code="80202",
            gate_code="4821#",
        )

        body = authed_client.get(LIST_URL).json()
        location = body["results"][0]["locations"][0]

        assert "gate_code" not in location
        assert "alarm_code" not in location
        assert location["one_line_address"] == "1 Elm St, Denver, CO, 80202"

    def test_the_detail_endpoint_does_not_expose_codes_either(
        self, authed_client, organization, make_customer
    ):
        """
        Reading a code requires the audited reveal action. An unlogged read on
        the detail endpoint would make the audit trail decorative.
        See audit/tests/test_reveal.py for the full contract.
        """
        customer = make_customer(organization)
        location = ServiceLocation.objects.create(
            organization=organization,
            customer=customer,
            line1="1 Elm St",
            city="Denver",
            state="CO",
            postal_code="80202",
            gate_code="4821#",
        )

        body = authed_client.get(reverse("customers:location-detail", args=[location.id])).json()

        assert "gate_code" not in body
        assert body["has_access_codes"] is True


@pytest.mark.django_db
class TestCustomerPermissions:
    def test_a_cleaner_cannot_list_customers(self, api_client, organization, make_member):
        api_client.force_login(make_member(organization, role=Role.CLEANER))

        assert api_client.get(LIST_URL).status_code == 403

    def test_a_customer_cannot_list_customers(self, api_client, organization, make_member):
        api_client.force_login(make_member(organization, role=Role.CUSTOMER))

        assert api_client.get(LIST_URL).status_code == 403

    def test_a_dispatcher_can(self, api_client, organization, make_member):
        api_client.force_login(make_member(organization, role=Role.DISPATCHER))

        assert api_client.get(LIST_URL).status_code == 200

    def test_anonymous_cannot(self, api_client, organization):
        assert api_client.get(LIST_URL).status_code == 403
