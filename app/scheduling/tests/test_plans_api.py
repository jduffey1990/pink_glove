"""
Recurring plans through the API.

The edit path is the interesting one. A plan edit has to reconcile eight weeks
of already-materialized jobs, and the response reports `regenerated` and `kept`
so the dispatcher can see what it did to the board (ADR-020).
"""

import datetime as dt

import pytest
from django.urls import reverse
from django.utils import timezone

from scheduling.models import Job, RecurringPlan
from scheduling.tests.factories import RecurringPlanFactory, ServiceLocationFactory

LIST = reverse("scheduling:plan-list")


def detail(plan):
    return reverse("scheduling:plan-detail", args=[plan.id])


def preview_url(plan):
    return reverse("scheduling:plan-preview", args=[plan.id])


def materialize_url(plan):
    return reverse("scheduling:plan-materialize", args=[plan.id])


@pytest.fixture
def payload(customer, location, service):
    return {
        "customer": str(customer.id),
        "location": str(location.id),
        "service": str(service.id),
        "rrule": "FREQ=WEEKLY;BYDAY=TU",
        "starts_on": timezone.now().date().isoformat(),
        "preferred_start_time": "09:00:00",
        "duration_minutes": 120,
    }


@pytest.mark.django_db
class TestCreating:
    def test_a_dispatcher_can_create_a_plan(self, dispatcher_client, payload):
        response = dispatcher_client.post(LIST, payload, format="json")

        assert response.status_code == 201
        assert RecurringPlan.objects.filter(id=response.json()["id"]).exists()

    def test_creating_materializes_immediately(self, dispatcher_client, payload):
        """A dispatcher who makes a plan expects to see the visits appear."""
        response = dispatcher_client.post(LIST, payload, format="json")

        assert Job.objects.filter(plan_id=response.json()["id"]).exists()

    def test_an_invalid_rrule_is_a_400(self, dispatcher_client, payload):
        response = dispatcher_client.post(
            LIST, {**payload, "rrule": "EVERY OTHER TUESDAY"}, format="json"
        )

        assert response.status_code == 400
        assert "rrule" in response.json()

    def test_a_dtstart_in_the_rule_is_refused(self, dispatcher_client, payload):
        """starts_on and preferred_start_time are the only source of the start."""
        response = dispatcher_client.post(
            LIST,
            {**payload, "rrule": "DTSTART:20270302T090000Z\nFREQ=WEEKLY;BYDAY=TU"},
            format="json",
        )

        assert response.status_code == 400
        assert "rrule" in response.json()

    def test_a_location_from_another_customer_is_refused(
        self, dispatcher_client, payload, organization
    ):
        stranger = ServiceLocationFactory(organization=organization)

        response = dispatcher_client.post(
            LIST, {**payload, "location": str(stranger.id)}, format="json"
        )

        assert response.status_code == 400
        assert "location" in response.json()

    def test_an_end_before_the_start_is_refused(self, dispatcher_client, payload):
        response = dispatcher_client.post(
            LIST,
            {
                **payload,
                "ends_on": (timezone.now().date() - dt.timedelta(days=1)).isoformat(),
            },
            format="json",
        )

        assert response.status_code == 400
        assert "ends_on" in response.json()

    def test_a_rival_organizations_cleaner_cannot_be_a_default_assignee(
        self, dispatcher_client, payload, rival_cleaner
    ):
        response = dispatcher_client.post(
            LIST, {**payload, "default_assignees": [str(rival_cleaner.id)]}, format="json"
        )

        assert response.status_code == 400

    def test_the_standing_crew_is_put_on_the_materialized_jobs(
        self, dispatcher_client, payload, cleaner
    ):
        response = dispatcher_client.post(
            LIST, {**payload, "default_assignees": [str(cleaner.id)]}, format="json"
        )

        jobs = Job.objects.filter(plan_id=response.json()["id"])
        assert jobs.exists()
        assert all(job.assignments.filter(user=cleaner).exists() for job in jobs)


@pytest.mark.django_db
class TestPermissions:
    def test_a_cleaner_cannot_read_plans(self, cleaner_client):
        assert cleaner_client.get(LIST).status_code == 403

    def test_a_customer_cannot_read_plans(self, customer_client):
        assert customer_client.get(LIST).status_code == 403

    def test_another_organizations_plan_is_a_404(self, dispatcher_client, other_organization):
        rival = RecurringPlanFactory(organization=other_organization)

        assert dispatcher_client.get(detail(rival)).status_code == 404


@pytest.mark.django_db
class TestPreview:
    def test_it_returns_the_next_occurrences_without_saving_them(
        self, dispatcher_client, organization
    ):
        plan = RecurringPlanFactory(
            organization=organization,
            rrule="FREQ=WEEKLY;BYDAY=TU",
            starts_on=timezone.now().date(),
            preferred_start_time=dt.time(9, 0),
        )
        before = Job.objects.count()

        response = dispatcher_client.get(preview_url(plan), {"count": 3})

        body = response.json()
        assert response.status_code == 200
        assert len(body["occurrences"]) == 3
        assert Job.objects.count() == before

    def test_each_occurrence_carries_both_forms(self, dispatcher_client, organization):
        """The UI shows local; anything comparing instants needs UTC."""
        plan = RecurringPlanFactory(organization=organization, starts_on=timezone.now().date())

        body = dispatcher_client.get(preview_url(plan), {"count": 1}).json()

        assert body["timezone"] == organization.timezone
        occurrence = body["occurrences"][0]
        assert "local" in occurrence
        assert "utc" in occurrence

    def test_a_monthly_rule_still_fills_the_preview(self, dispatcher_client, organization):
        """
        Six monthly occurrences reach beyond the 8-week materialization
        horizon, so the preview widens its own window rather than returning
        the two that happen to fall inside it.
        """
        plan = RecurringPlanFactory(
            organization=organization,
            rrule="FREQ=MONTHLY;BYMONTHDAY=1",
            starts_on=timezone.now().date(),
        )

        body = dispatcher_client.get(preview_url(plan), {"count": 6}).json()

        assert len(body["occurrences"]) == 6

    def test_the_count_is_capped(self, dispatcher_client, organization):
        plan = RecurringPlanFactory(
            organization=organization, rrule="FREQ=DAILY", starts_on=timezone.now().date()
        )

        body = dispatcher_client.get(preview_url(plan), {"count": 5000}).json()

        assert len(body["occurrences"]) <= 50

    def test_a_nonsense_count_falls_back_to_the_default(self, dispatcher_client, organization):
        plan = RecurringPlanFactory(
            organization=organization, rrule="FREQ=DAILY", starts_on=timezone.now().date()
        )

        body = dispatcher_client.get(preview_url(plan), {"count": "lots"}).json()

        assert len(body["occurrences"]) == 6


@pytest.mark.django_db
class TestMaterializeAction:
    def test_it_reports_how_many_it_created(self, dispatcher_client, organization):
        plan = RecurringPlanFactory(organization=organization, starts_on=timezone.now().date())

        response = dispatcher_client.post(materialize_url(plan))

        assert response.status_code == 200
        assert response.json()["created"] == Job.objects.filter(plan=plan).count()

    def test_running_it_again_creates_nothing(self, dispatcher_client, organization):
        plan = RecurringPlanFactory(organization=organization, starts_on=timezone.now().date())
        dispatcher_client.post(materialize_url(plan))

        assert dispatcher_client.post(materialize_url(plan)).json()["created"] == 0

    def test_an_unpriceable_plan_is_a_400(self, dispatcher_client, organization, customer):
        from decimal import Decimal

        from catalog.enums import PricingModel
        from scheduling.tests.factories import ServiceFactory

        plan = RecurringPlanFactory(
            organization=organization,
            customer=customer,
            location=ServiceLocationFactory(
                organization=organization, customer=customer, square_feet=None
            ),
            service=ServiceFactory(
                organization=organization,
                pricing_model=PricingModel.PER_SQFT,
                per_sqft_rate_cents=Decimal("10.000"),
            ),
            starts_on=timezone.now().date(),
        )

        response = dispatcher_client.post(materialize_url(plan))

        assert response.status_code == 400


@pytest.mark.django_db
class TestEditing:
    @pytest.fixture
    def live_plan(self, dispatcher_client, payload):
        response = dispatcher_client.post(LIST, payload, format="json")
        return RecurringPlan.objects.get(id=response.json()["id"])

    def test_changing_the_time_regenerates_and_reports(self, dispatcher_client, live_plan):
        response = dispatcher_client.patch(
            detail(live_plan), {"preferred_start_time": "14:00:00"}, format="json"
        )

        body = response.json()
        assert response.status_code == 200
        assert body["regenerated"] > 0
        assert body["kept"] == 0

    def test_the_future_jobs_move_to_the_new_time(self, dispatcher_client, live_plan):
        dispatcher_client.patch(
            detail(live_plan), {"preferred_start_time": "14:00:00"}, format="json"
        )

        tz = live_plan.organization.tz
        future = Job.objects.filter(plan=live_plan, scheduled_start__gt=timezone.now())
        assert future.exists()
        assert all(job.scheduled_start.astimezone(tz).hour == 14 for job in future)

    def test_a_rescheduled_job_is_kept_and_counted(self, dispatcher_client, live_plan):
        moved = (
            Job.objects.filter(plan=live_plan, scheduled_start__gt=timezone.now())
            .order_by("scheduled_start")
            .last()
        )
        moved.scheduled_start += dt.timedelta(days=1)
        moved.scheduled_end += dt.timedelta(days=1)
        moved.save()

        body = dispatcher_client.patch(
            detail(live_plan), {"preferred_start_time": "14:00:00"}, format="json"
        ).json()

        moved.refresh_from_db()
        assert body["kept"] == 1
        assert moved.deleted_at is None

    def test_editing_a_note_does_not_regenerate(self, dispatcher_client, live_plan):
        """Only fields that change *when* a visit happens invalidate the board."""
        before = set(Job.objects.filter(plan=live_plan).values_list("id", flat=True))

        body = dispatcher_client.patch(
            detail(live_plan), {"notes": "Ring the bell twice."}, format="json"
        ).json()

        after = set(Job.objects.filter(plan=live_plan).values_list("id", flat=True))
        assert "regenerated" not in body
        assert after == before

    def test_deactivating_clears_the_future_board(self, dispatcher_client, live_plan):
        response = dispatcher_client.patch(detail(live_plan), {"is_active": False}, format="json")

        assert response.json()["regenerated"] == 0
        assert not Job.objects.filter(plan=live_plan, scheduled_start__gt=timezone.now()).exists()

    def test_an_invalid_edit_leaves_the_jobs_alone(self, dispatcher_client, live_plan):
        before = set(Job.objects.filter(plan=live_plan).values_list("id", flat=True))

        response = dispatcher_client.patch(
            detail(live_plan), {"rrule": "NOT A RULE"}, format="json"
        )

        after = set(Job.objects.filter(plan=live_plan).values_list("id", flat=True))
        assert response.status_code == 400
        assert after == before
