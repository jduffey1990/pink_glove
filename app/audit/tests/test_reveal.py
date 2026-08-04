import pytest
from django.urls import reverse

from audit.models import ACCESS_WARNING, AccessReveal
from customers.models import Customer, ServiceLocation
from users.enums import Role


@pytest.fixture
def location(db, organization):
    customer = Customer.objects.create(organization=organization, first_name="Dana", last_name="Wu")
    return ServiceLocation.objects.create(
        organization=organization,
        customer=customer,
        label="Home",
        line1="1 Elm St",
        city="Denver",
        state="CO",
        postal_code="80202",
        gate_code="4821#",
        alarm_code="9930",
    )


def reveal_url(location):
    return reverse("customers:location-reveal-access", args=[location.id])


@pytest.mark.django_db
class TestCodesAreNotExposedNormally:
    def test_location_detail_does_not_return_codes(self, authed_client, location):
        body = authed_client.get(reverse("customers:location-detail", args=[location.id])).json()

        assert "gate_code" not in body
        assert "alarm_code" not in body
        assert "key_location" not in body

    def test_detail_reports_that_codes_exist_without_revealing_them(self, authed_client, location):
        body = authed_client.get(reverse("customers:location-detail", args=[location.id])).json()

        assert body["has_access_codes"] is True

    def test_codes_remain_writable(self, authed_client, location):
        response = authed_client.patch(
            reverse("customers:location-detail", args=[location.id]),
            {"gate_code": "1111"},
            format="json",
        )

        assert response.status_code == 200
        assert "gate_code" not in response.json()
        location.refresh_from_db()
        assert location.gate_code == "1111"

    def test_customer_list_does_not_leak_codes(self, authed_client, location):
        body = authed_client.get(reverse("customers:customer-list")).json()
        nested = body["results"][0]["locations"][0]

        assert "gate_code" not in nested


@pytest.mark.django_db
class TestReveal:
    def test_acknowledged_reveal_returns_the_codes(self, authed_client, location):
        response = authed_client.post(reveal_url(location), {"acknowledged": True}, format="json")

        assert response.status_code == 200
        assert response.json()["gate_code"] == "4821#"
        assert response.json()["alarm_code"] == "9930"

    def test_reveal_without_acknowledgement_is_refused_and_returns_the_warning(
        self, authed_client, location
    ):
        response = authed_client.post(reveal_url(location), {"acknowledged": False}, format="json")

        assert response.status_code == 400
        assert response.json()["detail"] == ACCESS_WARNING
        assert not AccessReveal.objects.exists()

    def test_acknowledgement_is_required_server_side(self, authed_client, location):
        """The warning is part of the contract, not a dialog the UI could drop."""
        response = authed_client.post(reveal_url(location), {}, format="json")

        assert response.status_code == 400
        assert not AccessReveal.objects.exists()

    def test_a_row_is_written_with_who_what_and_where(self, authed_client, location, owner):
        authed_client.post(reveal_url(location), {"acknowledged": True}, format="json")

        reveal = AccessReveal.objects.get()
        assert reveal.user == owner
        assert reveal.location == location
        assert reveal.organization == location.organization
        assert set(reveal.fields_revealed) == {"gate_code", "alarm_code"}
        assert reveal.acknowledged is True
        assert reveal.ip_address is not None

    def test_only_populated_fields_are_listed(self, authed_client, location):
        location.alarm_code = ""
        location.save()

        authed_client.post(reveal_url(location), {"acknowledged": True}, format="json")

        assert AccessReveal.objects.get().fields_revealed == ["gate_code"]

    def test_reveals_are_not_flagged_at_request_time(self, authed_client, location):
        """
        Flagging is an end-of-day judgement (Phase 3). Deciding at request time
        would bake in a verdict from a schedule that may still change.
        """
        authed_client.post(reveal_url(location), {"acknowledged": True}, format="json")

        reveal = AccessReveal.objects.get()
        assert reveal.is_flagged is False
        assert reveal.evaluated_at is None

    def test_a_cleaner_can_reveal(self, api_client, organization, location, make_member):
        """They are the ones at the door."""
        api_client.force_login(make_member(organization, role=Role.CLEANER))

        response = api_client.post(reveal_url(location), {"acknowledged": True}, format="json")

        assert response.status_code == 200
        assert AccessReveal.objects.get().fields_revealed

    def test_a_customer_cannot_reveal(self, api_client, organization, location, make_member):
        api_client.force_login(make_member(organization, role=Role.CUSTOMER))

        assert (
            api_client.post(reveal_url(location), {"acknowledged": True}, format="json").status_code
            == 403
        )

    def test_cannot_reveal_another_organizations_location(
        self, api_client, other_organization, location, make_member
    ):
        api_client.force_login(make_member(other_organization, role=Role.OWNER))

        response = api_client.post(reveal_url(location), {"acknowledged": True}, format="json")

        assert response.status_code == 404
        assert not AccessReveal.objects.exists()


@pytest.mark.django_db
class TestAppendOnly:
    def test_rows_refuse_deletion(self, authed_client, location):
        authed_client.post(reveal_url(location), {"acknowledged": True}, format="json")
        reveal = AccessReveal.objects.get()

        with pytest.raises(NotImplementedError):
            reveal.delete()

        with pytest.raises(NotImplementedError):
            reveal.hard_delete()

    def test_there_is_no_update_or_destroy_route(self, authed_client, location):
        authed_client.post(reveal_url(location), {"acknowledged": True}, format="json")
        reveal = AccessReveal.objects.get()
        url = reverse("audit:access-reveal-detail", args=[reveal.id])

        assert authed_client.patch(url, {"is_flagged": False}, format="json").status_code == 405
        assert authed_client.delete(url).status_code == 405


@pytest.mark.django_db
class TestAuditTrailAccess:
    def test_an_owner_can_read_the_trail(self, authed_client, location):
        authed_client.post(reveal_url(location), {"acknowledged": True}, format="json")

        body = authed_client.get(reverse("audit:access-reveal-list")).json()

        assert body["count"] == 1
        assert body["results"][0]["user_email"]
        assert body["results"][0]["customer_name"] == "Dana Wu"

    def test_a_cleaner_cannot_read_the_trail(self, api_client, organization, location, make_member):
        """The people being recorded should not be able to curate the record."""
        api_client.force_login(make_member(organization, role=Role.CLEANER))

        assert api_client.get(reverse("audit:access-reveal-list")).status_code == 403

    def test_a_dispatcher_cannot_read_the_trail(
        self, api_client, organization, location, make_member
    ):
        api_client.force_login(make_member(organization, role=Role.DISPATCHER))

        assert api_client.get(reverse("audit:access-reveal-list")).status_code == 403

    def test_the_trail_is_scoped_to_the_organization(
        self, api_client, authed_client, organization, other_organization, location, make_member
    ):
        authed_client.post(reveal_url(location), {"acknowledged": True}, format="json")

        api_client.force_login(make_member(other_organization, role=Role.OWNER))
        body = api_client.get(reverse("audit:access-reveal-list")).json()

        assert body["count"] == 0

    def test_reviewing_a_reveal_records_who_closed_it(self, authed_client, location, owner):
        authed_client.post(reveal_url(location), {"acknowledged": True}, format="json")
        reveal = AccessReveal.objects.get()

        response = authed_client.post(
            reverse("audit:access-reveal-review", args=[reveal.id]),
            {"review_note": "Job ran late, confirmed with dispatcher."},
            format="json",
        )

        assert response.status_code == 200
        reveal.refresh_from_db()
        assert reveal.reviewed_by == owner
        assert reveal.reviewed_at is not None
        assert "ran late" in reveal.review_note
