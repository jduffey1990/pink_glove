"""
Reading jobs: who sees what, and how dates are interpreted.

The two things worth guarding hardest are the role scoping table (a cleaner
must not be able to list the whole book) and the local-date filter, because
both fail silently -- an over-broad queryset looks like a working endpoint.
"""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from django.urls import reverse

from scheduling.models import Job, JobAssignment
from scheduling.tests.factories import CustomerFactory, JobFactory, ServiceLocationFactory

DENVER = ZoneInfo("America/Denver")

LIST = reverse("scheduling:job-list")


def detail(job):
    return reverse("scheduling:job-detail", args=[job.id])


@pytest.mark.django_db
class TestTenantIsolation:
    def test_another_organizations_job_is_a_404_not_a_403(self, dispatcher_client, rival_job):
        """403 would confirm the job exists. 404 says nothing."""
        assert dispatcher_client.get(detail(rival_job)).status_code == 404

    def test_the_list_never_includes_another_organizations_jobs(
        self, dispatcher_client, job, rival_job
    ):
        response = dispatcher_client.get(LIST)

        ids = {row["id"] for row in response.json()["results"]}
        assert str(job.id) in ids
        assert str(rival_job.id) not in ids

    def test_anonymous_callers_are_refused(self, api_client, job):
        assert api_client.get(LIST).status_code == 403


@pytest.mark.django_db
class TestRoleScoping:
    def test_a_dispatcher_sees_every_job_in_the_organization(
        self, dispatcher_client, organization, job
    ):
        JobFactory(organization=organization)

        response = dispatcher_client.get(LIST)

        assert response.json()["count"] == 2

    def test_a_cleaner_sees_only_jobs_they_are_assigned_to(
        self, cleaner_client, organization, assigned_job
    ):
        JobFactory(organization=organization)  # someone else's

        response = cleaner_client.get(LIST)

        assert [row["id"] for row in response.json()["results"]] == [str(assigned_job.id)]

    def test_a_cleaner_taken_off_a_job_stops_seeing_it(self, cleaner_client, cleaner, assigned_job):
        """Unassigning soft-deletes the row, and a join does not filter soft deletes."""
        JobAssignment.objects.get(job=assigned_job, user=cleaner).delete()

        assert cleaner_client.get(LIST).json()["count"] == 0
        assert cleaner_client.get(LIST, {"mine": "true"}).json()["count"] == 0
        assert cleaner_client.get(detail(assigned_job)).status_code == 404

    def test_the_assignee_filter_ignores_removed_assignments(
        self, dispatcher_client, cleaner, assigned_job
    ):
        JobAssignment.objects.get(job=assigned_job, user=cleaner).delete()

        response = dispatcher_client.get(LIST, {"assignee": str(cleaner.id)})

        assert response.json()["count"] == 0

    def test_a_cleaner_cannot_open_a_job_they_are_not_on(self, cleaner_client, organization, job):
        assert cleaner_client.get(detail(job)).status_code == 404

    def test_a_customer_sees_only_their_own_jobs(self, customer_client, organization, job, service):
        other_customer = CustomerFactory(organization=organization)
        JobFactory(
            organization=organization,
            customer=other_customer,
            location=ServiceLocationFactory(organization=organization, customer=other_customer),
            service=service,
        )

        response = customer_client.get(LIST)

        assert [row["id"] for row in response.json()["results"]] == [str(job.id)]

    def test_a_cleaner_sees_a_job_once_even_with_two_assignments(
        self, cleaner_client, organization, assigned_job, other_cleaner
    ):
        """A join against assignments duplicates rows without a distinct()."""
        JobAssignment.objects.create(
            organization=organization, job=assigned_job, user=other_cleaner
        )

        response = cleaner_client.get(LIST)

        assert response.json()["count"] == 1


@pytest.mark.django_db
class TestCustomerSerializerShape:
    def test_a_customer_does_not_see_the_crew_or_the_internal_notes(
        self, customer_client, assigned_job
    ):
        assigned_job.notes = "Customer is difficult about the dog."
        assigned_job.save()

        row = customer_client.get(detail(assigned_job)).json()

        assert "assignments" not in row
        assert "notes" not in row

    def test_a_dispatcher_does_see_them(self, dispatcher_client, assigned_job):
        row = dispatcher_client.get(detail(assigned_job)).json()

        assert len(row["assignments"]) == 1
        assert "notes" in row


@pytest.mark.django_db
class TestNestedSummaries:
    def test_a_cleaner_gets_what_they_need_without_the_customer_endpoints(
        self, cleaner_client, assigned_job, location
    ):
        """
        The customer and location endpoints are dispatcher-only. Nesting the
        summaries here is what keeps a cleaner's phone to one request.
        """
        location.access_notes = "Side gate, then the back door."
        location.gate_code = "1234"
        location.save()

        row = cleaner_client.get(detail(assigned_job)).json()

        assert row["location_detail"]["access_notes"] == "Side gate, then the back door."
        assert row["location_detail"]["has_access_codes"] is True
        assert row["customer_detail"]["display_name"]
        assert row["service_detail"]["name"]

    def test_the_access_codes_are_never_in_the_payload(
        self, cleaner_client, assigned_job, location
    ):
        """Codes come only from the audited reveal action (ADR-016)."""
        # Letters outside a-f: a plain "1234" turns up by chance inside the
        # UUIDs and timestamps of any payload, and the test went red on that.
        location.gate_code = "GATE-ZQ71"
        location.alarm_code = "ALARM-XK93"
        location.save()

        body = cleaner_client.get(detail(assigned_job)).content.decode()

        assert "GATE-ZQ71" not in body
        assert "ALARM-XK93" not in body

    def test_open_time_entry_is_the_callers_own(
        self, cleaner_client, dispatcher_client, assigned_job
    ):
        cleaner_client.post(reverse("scheduling:job-clock-in", args=[assigned_job.id]))

        as_cleaner = cleaner_client.get(detail(assigned_job)).json()
        as_dispatcher = dispatcher_client.get(detail(assigned_job)).json()

        assert as_cleaner["open_time_entry"] is not None
        assert as_dispatcher["open_time_entry"] is None


@pytest.mark.django_db
class TestLocalDateFiltering:
    """
    `date_from` / `date_to` are organization-local dates. The frontend sends
    the day a dispatcher clicked and never does timezone arithmetic itself.
    """

    @pytest.fixture
    def late_night_job(self, organization, customer, location, service):
        """23:30 on the 14th in Denver -- which is the 15th in UTC."""
        start = dt.datetime(2027, 6, 14, 23, 30, tzinfo=DENVER)
        return JobFactory(
            organization=organization,
            customer=customer,
            location=location,
            service=service,
            scheduled_start=start,
            scheduled_end=start + dt.timedelta(hours=1),
        )

    def test_a_late_night_job_belongs_to_its_local_day(self, dispatcher_client, late_night_job):
        assert late_night_job.scheduled_start.astimezone(dt.UTC).date() == dt.date(2027, 6, 15)

        response = dispatcher_client.get(LIST, {"date_from": "2027-06-14", "date_to": "2027-06-14"})

        assert [row["id"] for row in response.json()["results"]] == [str(late_night_job.id)]

    def test_it_is_not_returned_for_the_following_local_day(
        self, dispatcher_client, late_night_job
    ):
        response = dispatcher_client.get(LIST, {"date_from": "2027-06-15", "date_to": "2027-06-15"})

        assert response.json()["count"] == 0

    def test_a_range_is_inclusive_of_both_ends(
        self, dispatcher_client, organization, customer, location, service
    ):
        for day in (10, 11, 12):
            start = dt.datetime(2027, 6, day, 9, 0, tzinfo=DENVER)
            JobFactory(
                organization=organization,
                customer=customer,
                location=location,
                service=service,
                scheduled_start=start,
                scheduled_end=start + dt.timedelta(hours=2),
            )

        response = dispatcher_client.get(LIST, {"date_from": "2027-06-10", "date_to": "2027-06-12"})

        assert response.json()["count"] == 3

    def test_a_range_longer_than_the_cap_is_refused(self, dispatcher_client):
        response = dispatcher_client.get(LIST, {"date_from": "2027-01-01", "date_to": "2027-12-31"})

        assert response.status_code == 400
        assert "date_to" in response.json()

    def test_a_backwards_range_is_refused(self, dispatcher_client):
        response = dispatcher_client.get(LIST, {"date_from": "2027-06-10", "date_to": "2027-06-01"})

        assert response.status_code == 400


@pytest.mark.django_db
class TestOtherFilters:
    def test_filter_by_status(self, dispatcher_client, organization, job):
        other = JobFactory(organization=organization)
        other.status = "cancelled"
        other.save()

        response = dispatcher_client.get(LIST, {"status": "scheduled"})

        assert [row["id"] for row in response.json()["results"]] == [str(job.id)]

    def test_status_accepts_several_values(self, dispatcher_client, organization, job):
        other = JobFactory(organization=organization)
        other.status = "cancelled"
        other.save()

        response = dispatcher_client.get(LIST, {"status": ["scheduled", "cancelled"]})

        assert response.json()["count"] == 2

    def test_filter_by_assignee(self, dispatcher_client, assigned_job, cleaner, organization):
        JobFactory(organization=organization)

        response = dispatcher_client.get(LIST, {"assignee": str(cleaner.id)})

        assert [row["id"] for row in response.json()["results"]] == [str(assigned_job.id)]

    def test_mine_returns_the_callers_own_jobs(self, cleaner_client, assigned_job, organization):
        JobFactory(organization=organization)

        response = cleaner_client.get(LIST, {"mine": "true"})

        assert [row["id"] for row in response.json()["results"]] == [str(assigned_job.id)]

    def test_filter_by_customer(self, dispatcher_client, organization, job, customer):
        JobFactory(organization=organization)

        response = dispatcher_client.get(LIST, {"customer": str(customer.id)})

        assert [row["id"] for row in response.json()["results"]] == [str(job.id)]


@pytest.mark.django_db
class TestWriting:
    def _payload(self, customer, location, service):
        start = dt.datetime(2027, 6, 10, 9, 0, tzinfo=DENVER)
        return {
            "customer": str(customer.id),
            "location": str(location.id),
            "service": str(service.id),
            "scheduled_start": start.isoformat(),
            "scheduled_end": (start + dt.timedelta(hours=2)).isoformat(),
        }

    def test_a_dispatcher_can_create_a_job(self, dispatcher_client, customer, location, service):
        response = dispatcher_client.post(
            LIST, self._payload(customer, location, service), format="json"
        )

        assert response.status_code == 201
        assert Job.objects.filter(id=response.json()["id"]).exists()

    def test_the_price_is_quoted_when_omitted(self, dispatcher_client, customer, location, service):
        response = dispatcher_client.post(
            LIST, self._payload(customer, location, service), format="json"
        )

        assert response.json()["price_cents"] == service.base_price_cents

    def test_an_explicit_price_is_kept(self, dispatcher_client, customer, location, service):
        payload = {**self._payload(customer, location, service), "price_cents": 4242}

        response = dispatcher_client.post(LIST, payload, format="json")

        assert response.json()["price_cents"] == 4242

    def test_a_per_sqft_service_without_square_footage_names_the_field(
        self, dispatcher_client, organization, customer
    ):
        """A 400 naming square_feet, never a 500 out of the pricing model."""
        from decimal import Decimal

        from catalog.enums import PricingModel
        from scheduling.tests.factories import ServiceFactory

        sqft_service = ServiceFactory(
            organization=organization,
            pricing_model=PricingModel.PER_SQFT,
            per_sqft_rate_cents=Decimal("10.000"),
        )
        bare_location = ServiceLocationFactory(
            organization=organization, customer=customer, square_feet=None
        )

        response = dispatcher_client.post(
            LIST, self._payload(customer, bare_location, sqft_service), format="json"
        )

        assert response.status_code == 400
        assert "square_feet" in response.json()

    def test_a_cleaner_cannot_create_a_job(self, cleaner_client, customer, location, service):
        response = cleaner_client.post(
            LIST, self._payload(customer, location, service), format="json"
        )

        assert response.status_code == 403

    def test_a_location_from_another_customer_is_refused(
        self, dispatcher_client, organization, customer, service
    ):
        stranger_location = ServiceLocationFactory(organization=organization)

        response = dispatcher_client.post(
            LIST, self._payload(customer, stranger_location, service), format="json"
        )

        assert response.status_code == 400
        assert "location" in response.json()

    def test_another_organizations_location_is_refused(
        self, dispatcher_client, customer, service, other_organization
    ):
        rival_location = ServiceLocationFactory(organization=other_organization)

        response = dispatcher_client.post(
            LIST, self._payload(customer, rival_location, service), format="json"
        )

        assert response.status_code == 400

    def test_an_end_before_the_start_is_refused(
        self, dispatcher_client, customer, location, service
    ):
        payload = self._payload(customer, location, service)
        payload["scheduled_end"] = payload["scheduled_start"]

        response = dispatcher_client.post(LIST, payload, format="json")

        assert response.status_code == 400
        assert "scheduled_end" in response.json()

    def test_status_cannot_be_set_through_a_plain_update(self, dispatcher_client, job):
        """The state machine lives in one place: the status action."""
        response = dispatcher_client.patch(detail(job), {"status": "complete"}, format="json")

        job.refresh_from_db()
        assert response.status_code == 200
        assert job.status == "scheduled"

    def test_the_organization_cannot_be_moved_by_a_payload(
        self, dispatcher_client, job, other_organization
    ):
        dispatcher_client.patch(
            detail(job), {"organization": str(other_organization.id)}, format="json"
        )

        job.refresh_from_db()
        assert job.organization_id != other_organization.id


@pytest.mark.django_db
class TestDeleting:
    def test_a_dispatcher_can_delete_an_unworked_job(self, dispatcher_client, job):
        response = dispatcher_client.delete(detail(job))

        job.refresh_from_db()
        assert response.status_code == 204
        assert job.deleted_at is not None

    def test_a_job_with_recorded_time_is_a_409(
        self, dispatcher_client, cleaner_client, assigned_job
    ):
        """Deleting it would orphan the hours. Cancelling keeps the record."""
        cleaner_client.post(reverse("scheduling:job-clock-in", args=[assigned_job.id]))

        response = dispatcher_client.delete(detail(assigned_job))

        assigned_job.refresh_from_db()
        assert response.status_code == 409
        assert "cancel" in response.json()["detail"].lower()
        assert assigned_job.deleted_at is None

    def test_a_cleaner_cannot_delete_a_job(self, cleaner_client, assigned_job):
        assert cleaner_client.delete(detail(assigned_job)).status_code == 403
