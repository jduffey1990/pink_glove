"""
What the plan wrote, and so what a person changed (ADR-032, amending ADR-020).

ADR-020 keeps a plan visit somebody touched and rebuilds the rest, and used
to read "touched" off the start alone. A price set by hand, a longer slot or
a note was thrown away by the next plan edit.

"Did somebody change the price" cannot be answered by re-quoting: a service's
price, a location's square footage and a plan's note all change without
regenerating anything, so one plan's untouched visits legitimately carry two
prices and two notes. So the materializer stamps each visit with what it
wrote (`planned_price_cents`, `planned_end`, `planned_notes`), and a visit is
untouched while it still says what its stamp says.

Both sides of that line are tested here, and the second matters as much as
the first: a hand edit survives, and nothing is pinned that a person did not
touch.
"""

import datetime as dt
from decimal import Decimal

import pytest
from django.contrib import admin
from django.urls import reverse
from django.utils import timezone

from catalog.enums import PricingModel
from scheduling.enums import JobStatus
from scheduling.models import Job, JobAssignment
from scheduling.services import materialize_plan, regenerate_plan
from scheduling.tests.factories import (
    JobFactory,
    RecurringPlanFactory,
    ServiceFactory,
    ServiceLocationFactory,
)

LIST = reverse("scheduling:job-list")


def detail(job):
    return reverse("scheduling:job-detail", args=[job.id])


def plan_url(plan, action=None):
    if action:
        return reverse(f"scheduling:plan-{action}", args=[plan.id])
    return reverse("scheduling:plan-detail", args=[plan.id])


def planned_day(job):
    return job.plan_occurrence.astimezone(job.organization.tz).date()


def future_of(plan):
    return Job.objects.filter(plan=plan, scheduled_start__gt=timezone.now()).order_by(
        "scheduled_start"
    )


def board(plan):
    """Everything that distinguishes one run's result from another's, ids aside."""
    return sorted(
        Job.objects.filter(plan=plan).values_list(
            "plan_occurrence",
            "scheduled_start",
            "scheduled_end",
            "status",
            "price_cents",
            "notes",
            "planned_price_cents",
            "planned_end",
            "planned_notes",
        )
    )


@pytest.fixture
def plan(organization, customer, location, service):
    plan = RecurringPlanFactory(
        organization=organization,
        customer=customer,
        location=location,
        service=service,
        rrule="FREQ=DAILY",
        starts_on=organization.today(),
        preferred_start_time=dt.time(9, 0),
        duration_minutes=120,
        notes="Ring the bell.",
    )
    materialize_plan(plan)
    return plan


@pytest.fixture
def visit(plan):
    """A visit a week out, so nothing about it is close to `now`."""
    cutoff = timezone.now() + dt.timedelta(days=7)
    return (
        Job.objects.filter(plan=plan, scheduled_start__gt=cutoff)
        .order_by("scheduled_start")
        .first()
    )


@pytest.mark.django_db
class TestTheStamp:
    def test_a_materialized_visit_is_stamped_with_what_the_plan_wrote(self, plan):
        for job in Job.objects.filter(plan=plan):
            assert job.planned_price_cents == job.price_cents == 15000
            assert job.planned_end == job.scheduled_end
            assert job.planned_end == job.plan_occurrence + dt.timedelta(minutes=120)
            assert job.planned_notes == job.notes == "Ring the bell."

    def test_a_plan_with_no_note_stamps_an_empty_one_not_a_missing_one(self, organization):
        plan = RecurringPlanFactory(organization=organization, notes="")
        materialize_plan(plan)

        assert set(Job.objects.filter(plan=plan).values_list("planned_notes", flat=True)) == {""}

    def test_a_one_off_visit_has_none(self, dispatcher_client, customer, location, service):
        start = timezone.now().replace(second=0, microsecond=0) + dt.timedelta(days=3)
        row = dispatcher_client.post(
            LIST,
            {
                "customer": str(customer.id),
                "location": str(location.id),
                "service": str(service.id),
                "scheduled_start": start.isoformat(),
                "scheduled_end": (start + dt.timedelta(hours=2)).isoformat(),
            },
            format="json",
        ).json()

        job = Job.objects.get(id=row["id"])
        assert job.planned_price_cents is None
        assert job.planned_end is None
        assert job.planned_notes is None

    def test_a_regenerated_visit_carries_the_new_definitions_stamp(
        self, dispatcher_client, plan, visit
    ):
        dispatcher_client.patch(
            plan_url(plan), {"duration_minutes": 180, "price_override_cents": 9900}, format="json"
        )

        rebuilt = Job.objects.get(plan=plan, plan_occurrence=visit.plan_occurrence)
        assert rebuilt.pk != visit.pk
        assert rebuilt.planned_end == rebuilt.scheduled_end
        assert rebuilt.duration_minutes == 180
        assert rebuilt.planned_price_cents == rebuilt.price_cents == 9900

    def test_it_is_not_on_the_api(self, dispatcher_client, visit):
        row = dispatcher_client.get(detail(visit)).json()

        assert not [key for key in row if key.startswith("planned_")]


@pytest.mark.django_db
class TestTheStampCannotBeWritten:
    FORGED = {
        "planned_price_cents": 1,
        "planned_end": "2031-01-01T00:00:00Z",
        "planned_notes": "forged",
    }

    def test_a_patch_cannot_move_it(self, dispatcher_client, visit):
        before = (visit.planned_price_cents, visit.planned_end, visit.planned_notes)

        response = dispatcher_client.patch(detail(visit), self.FORGED, format="json")

        visit.refresh_from_db()
        assert response.status_code == 200
        assert (visit.planned_price_cents, visit.planned_end, visit.planned_notes) == before

    def test_a_patch_cannot_launder_a_hand_edit_into_looking_untouched(
        self, dispatcher_client, plan, visit
    ):
        """Set the price and claim the plan wrote it. The visit must still be kept."""
        dispatcher_client.patch(
            detail(visit), {"price_cents": 4242, "planned_price_cents": 4242}, format="json"
        )

        body = dispatcher_client.post(plan_url(plan, "regenerate")).json()

        visit.refresh_from_db()
        assert body["kept"] == 1
        assert visit.deleted_at is None

    def test_a_create_cannot_set_it(self, dispatcher_client, customer, location, service):
        start = timezone.now().replace(second=0, microsecond=0) + dt.timedelta(days=3)
        row = dispatcher_client.post(
            LIST,
            {
                "customer": str(customer.id),
                "location": str(location.id),
                "service": str(service.id),
                "scheduled_start": start.isoformat(),
                "scheduled_end": (start + dt.timedelta(hours=2)).isoformat(),
                **self.FORGED,
            },
            format="json",
        ).json()

        job = Job.objects.get(id=row["id"])
        assert (job.planned_price_cents, job.planned_end, job.planned_notes) == (None, None, None)

    def test_the_admin_shows_it_and_cannot_edit_it(self):
        readonly = admin.site._registry[Job].readonly_fields

        assert {"planned_price_cents", "planned_end", "planned_notes"} <= set(readonly)


@pytest.mark.django_db
class TestAHandEditThatLeavesTheStartAloneIsKept:
    """Each of these was rebuilt, and the edit lost, before the stamp."""

    def test_a_price_set_by_hand_survives_a_plan_edit(self, dispatcher_client, plan, visit):
        dispatcher_client.patch(detail(visit), {"price_cents": 99900}, format="json")

        body = dispatcher_client.patch(
            plan_url(plan), {"preferred_start_time": "14:00:00"}, format="json"
        ).json()

        visit.refresh_from_db()
        assert body["kept"] == 1
        assert visit.deleted_at is None
        assert visit.price_cents == 99900

    def test_a_longer_slot_survives(self, dispatcher_client, plan, visit):
        longer = visit.scheduled_end + dt.timedelta(hours=1)
        dispatcher_client.patch(detail(visit), {"scheduled_end": longer.isoformat()}, format="json")

        body = dispatcher_client.post(plan_url(plan, "regenerate")).json()

        visit.refresh_from_db()
        assert body["kept"] == 1
        assert visit.deleted_at is None
        assert visit.scheduled_end == longer

    def test_a_note_survives(self, dispatcher_client, plan, visit):
        dispatcher_client.patch(detail(visit), {"notes": "Bring the ladder."}, format="json")

        body = dispatcher_client.patch(
            plan_url(plan), {"duration_minutes": 180}, format="json"
        ).json()

        visit.refresh_from_db()
        assert body["kept"] == 1
        assert visit.deleted_at is None
        assert visit.notes == "Bring the ladder."
        assert visit.duration_minutes == 120

    def test_the_neighbours_are_still_rebuilt(self, dispatcher_client, plan, visit):
        """So the visit is kept because of its edit, not because nothing regenerated."""
        neighbours = set(future_of(plan).exclude(pk=visit.pk).values_list("pk", flat=True))
        dispatcher_client.patch(detail(visit), {"price_cents": 99900}, format="json")

        body = dispatcher_client.patch(
            plan_url(plan), {"preferred_start_time": "14:00:00"}, format="json"
        ).json()

        assert body["regenerated"] == len(neighbours)
        assert not Job.objects.filter(pk__in=neighbours).exists()

    @pytest.mark.parametrize(
        ("edit", "undo"),
        [
            ({"price_cents": 99900}, {"price_cents": 15000}),
            ({"notes": "Bring the ladder."}, {"notes": "Ring the bell."}),
        ],
    )
    def test_an_edit_that_is_taken_back_gives_the_visit_back_to_the_plan(
        self, dispatcher_client, plan, visit, edit, undo
    ):
        """
        Derived from what the visit says now, not from a record that somebody
        once touched it. Put it back and it is an ordinary plan visit again.
        """
        dispatcher_client.patch(detail(visit), edit, format="json")
        dispatcher_client.patch(detail(visit), undo, format="json")

        body = dispatcher_client.post(plan_url(plan, "regenerate")).json()

        assert body["kept"] == 0
        assert not Job.objects.filter(pk=visit.pk).exists()

    def test_a_slot_put_back_to_its_length_gives_the_visit_back_too(
        self, dispatcher_client, plan, visit
    ):
        original = visit.scheduled_end
        longer = original + dt.timedelta(hours=1)
        dispatcher_client.patch(detail(visit), {"scheduled_end": longer.isoformat()}, format="json")
        dispatcher_client.patch(
            detail(visit), {"scheduled_end": original.isoformat()}, format="json"
        )

        assert dispatcher_client.post(plan_url(plan, "regenerate")).json()["kept"] == 0

    def test_a_patch_that_changes_nothing_pins_nothing(self, dispatcher_client, plan, visit):
        """The reason `updated_at` was not used as the signal: this bumps it."""
        dispatcher_client.patch(detail(visit), {}, format="json")
        dispatcher_client.patch(detail(visit), {"notes": "Ring the bell."}, format="json")

        assert dispatcher_client.post(plan_url(plan, "regenerate")).json()["kept"] == 0

    def test_a_visit_with_no_stamp_is_kept_which_is_the_safe_direction(
        self, dispatcher_client, plan, visit
    ):
        """
        Not knowing what the plan wrote is not evidence that nobody changed
        it. Keeping costs a stale visit the dispatcher can see in `kept`;
        rebuilding could cost a price somebody agreed.
        """
        Job.objects.filter(pk=visit.pk).update(
            planned_price_cents=None, planned_end=None, planned_notes=None
        )

        body = dispatcher_client.post(plan_url(plan, "regenerate")).json()

        assert body["kept"] == 1
        assert Job.objects.filter(pk=visit.pk).exists()

    def test_a_hand_added_assignee_is_still_not_a_pin(
        self, dispatcher_client, plan, visit, cleaner
    ):
        """
        ADR-020's accepted cost, unchanged and written down so it stays a
        decision: the crew is not stamped, so somebody added by hand to an
        otherwise untouched visit is lost when the plan is next edited.
        """
        dispatcher_client.post(
            reverse("scheduling:job-assign", args=[visit.id]),
            {"user": str(cleaner.id)},
            format="json",
        )

        body = dispatcher_client.post(plan_url(plan, "regenerate")).json()

        rebuilt = Job.objects.get(plan=plan, plan_occurrence=visit.plan_occurrence)
        assert body["kept"] == 0
        assert rebuilt.pk != visit.pk
        assert not JobAssignment.objects.filter(job=rebuilt).exists()


@pytest.mark.django_db
class TestNothingIsPinnedThatNobodyTouched:
    """
    The probe that settled the design, as a test.

    Raise the service's price, edit the plan's note, and let a later nightly
    run extend the horizon: the plan's untouched visits now carry two prices
    and two notes, and every one of them is exactly what the plan wrote on
    the day it wrote it. Comparing against a re-quote, or against the plan's
    current note, calls the older ones hand-edited and pins all of them.
    """

    @pytest.fixture
    def mixed(self, dispatcher_client, organization, plan, service):
        service.base_price_cents = 20000
        service.save()
        response = dispatcher_client.patch(
            plan_url(plan), {"notes": "Use the side gate."}, format="json"
        )
        assert "regenerated" not in response.json()

        plan.refresh_from_db()
        added = materialize_plan(plan, today=organization.today() + dt.timedelta(days=7))
        assert added == 7

        untouched = future_of(plan)
        assert {job.price_cents for job in untouched} == {15000, 20000}
        assert {job.notes for job in untouched} == {"Ring the bell.", "Use the side gate."}
        return plan

    @pytest.fixture
    def edits(self, organization, customer):
        return {
            "rule": {"rrule": "FREQ=WEEKLY;BYDAY=MO,WE,FR"},
            "time": {"preferred_start_time": "14:00:00"},
            "duration": {"duration_minutes": 180},
            "service": {"service": str(ServiceFactory(organization=organization).id)},
            "location": {
                "location": str(
                    ServiceLocationFactory(organization=organization, customer=customer).id
                )
            },
            "price override": {"price_override_cents": 9900},
        }

    @pytest.mark.parametrize(
        "which", ["rule", "time", "duration", "service", "location", "price override"]
    )
    def test_a_plan_edit_still_rebuilds_every_untouched_visit(
        self, dispatcher_client, mixed, edits, which
    ):
        before = set(future_of(mixed).values_list("pk", flat=True))
        assert len(before) > 56

        body = dispatcher_client.patch(plan_url(mixed), edits[which], format="json").json()

        after = future_of(mixed)
        assert body["kept"] == 0
        assert not Job.objects.filter(pk__in=before).exists()
        # Every visit now on the board is one this edit built.
        assert body["regenerated"] == after.count() > 0
        assert all(job.notes == "Use the side gate." for job in after)

    def test_the_rebuilt_visits_are_priced_as_of_today(self, dispatcher_client, mixed):
        dispatcher_client.patch(plan_url(mixed), {"duration_minutes": 180}, format="json")

        assert {job.price_cents for job in future_of(mixed)} == {20000}

    def test_a_price_rise_does_not_touch_existing_visits_or_make_them_look_edited(
        self, dispatcher_client, plan, service
    ):
        before = board(plan)
        service.base_price_cents = 20000
        service.save()

        assert board(plan) == before
        assert dispatcher_client.post(plan_url(plan, "regenerate")).json()["kept"] == 0

    def test_a_square_footage_change_does_not_either(
        self, dispatcher_client, organization, customer, location
    ):
        per_sqft = ServiceFactory(
            organization=organization,
            pricing_model=PricingModel.PER_SQFT,
            per_sqft_rate_cents=Decimal("10.000"),
            base_price_cents=0,
        )
        plan = RecurringPlanFactory(
            organization=organization,
            customer=customer,
            location=location,
            service=per_sqft,
            rrule="FREQ=DAILY",
            starts_on=organization.today(),
        )
        materialize_plan(plan)
        before = board(plan)
        assert {row[4] for row in before} == {20000}

        location.square_feet = 3000
        location.save()

        assert board(plan) == before
        body = dispatcher_client.post(plan_url(plan, "regenerate")).json()
        assert body["kept"] == 0
        assert {job.price_cents for job in future_of(plan)} == {30000}


@pytest.mark.django_db
class TestAKeptVisitAndAPlanWhoseTimeMoves:
    """
    Together with `_rekey_kept_jobs`. A visit kept for its price is still
    sitting at its planned instant, which is the one case the re-key had not
    met: it must stay where it is, account for its day, and not be joined by
    a new visit at the new time.
    """

    def test_it_keeps_its_old_time_and_its_day_gets_no_second_visit(
        self, dispatcher_client, plan, visit
    ):
        day, was_at = planned_day(visit), visit.scheduled_start
        dispatcher_client.patch(detail(visit), {"price_cents": 99900}, format="json")

        dispatcher_client.patch(plan_url(plan), {"preferred_start_time": "14:00:00"}, format="json")

        visit.refresh_from_db()
        that_day = [job for job in Job.objects.filter(plan=plan) if planned_day(job) == day]
        assert [job.pk for job in that_day] == [visit.pk]
        assert visit.scheduled_start == was_at
        assert visit.price_cents == 99900
        # Re-keyed to the new occurrence on its day, which is why nothing joined it.
        assert visit.plan_occurrence.astimezone(visit.organization.tz).hour == 14

    def test_it_is_still_kept_by_the_edit_after_that(self, dispatcher_client, plan, visit):
        dispatcher_client.patch(detail(visit), {"price_cents": 99900}, format="json")
        dispatcher_client.patch(plan_url(plan), {"preferred_start_time": "14:00:00"}, format="json")

        body = dispatcher_client.patch(
            plan_url(plan), {"preferred_start_time": "08:30:00"}, format="json"
        ).json()

        visit.refresh_from_db()
        assert body["kept"] == 1
        assert visit.deleted_at is None
        assert visit.price_cents == 99900

    def test_a_cancelled_visit_with_a_note_does_not_come_back(self, dispatcher_client, plan, visit):
        day = planned_day(visit)
        dispatcher_client.patch(detail(visit), {"notes": "Customer away."}, format="json")
        dispatcher_client.post(
            reverse("scheduling:job-status", args=[visit.id]),
            {"status": "cancelled", "reason": "Customer is away."},
            format="json",
        )

        dispatcher_client.patch(plan_url(plan), {"preferred_start_time": "14:00:00"}, format="json")

        that_day = [job for job in Job.objects.filter(plan=plan) if planned_day(job) == day]
        assert [job.status for job in that_day] == [JobStatus.CANCELLED]

    def test_tonights_run_adds_nothing(self, dispatcher_client, plan, visit):
        dispatcher_client.patch(detail(visit), {"price_cents": 99900}, format="json")
        dispatcher_client.patch(plan_url(plan), {"preferred_start_time": "14:00:00"}, format="json")
        plan.refresh_from_db()

        assert materialize_plan(plan) == 0
        assert materialize_plan(plan) == 0

    def test_twice_over_the_same_input_gives_the_same_board(self, dispatcher_client, plan, visit):
        """Regeneration and materialization, each run twice, and diffed."""
        dispatcher_client.patch(detail(visit), {"price_cents": 99900}, format="json")
        other = future_of(plan).exclude(pk=visit.pk).last()
        dispatcher_client.patch(detail(other), {"notes": "Bring the ladder."}, format="json")
        dispatcher_client.patch(plan_url(plan), {"preferred_start_time": "14:00:00"}, format="json")
        plan.refresh_from_db()

        first = board(plan)
        first_result = regenerate_plan(plan)
        second = board(plan)
        second_result = regenerate_plan(plan)
        materialize_plan(plan)
        third = board(plan)

        assert first == second == third
        assert first_result == second_result
        assert first_result["kept"] == 2


@pytest.mark.django_db
class TestVisitsThatAreNotThePlans:
    def test_a_one_off_visit_is_never_regenerated(self, organization, plan):
        one_off = JobFactory(organization=organization)

        regenerate_plan(plan)

        assert Job.objects.filter(pk=one_off.pk).exists()
