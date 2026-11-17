"""
The backfill in `0003_plan_stamps`, run for real.

Rows are made at the current schema, the migration is taken back out (which
drops the stamp columns and so forgets every stamp), and then run forward
again. What comes back is the backfill's work and nothing else: there is no
materializer in between to stamp anything.

Transactional, because a migration cannot run inside the test's own
transaction. It always finishes by migrating to the newest state, pass or
fail, so no later test meets a half-migrated database.
"""

import datetime as dt

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from scheduling.models import Job
from scheduling.services import materialize_plan
from scheduling.tests.factories import JobFactory, OrganizationFactory, RecurringPlanFactory

BEFORE = [("scheduling", "0002_upload_size_limits")]
AFTER = [("scheduling", "0003_plan_stamps")]


def migrate(targets):
    executor = MigrationExecutor(connection)
    executor.migrate(targets)


@pytest.fixture
def rewound():
    """Yield with the stamp migration unapplied; put everything back afterwards."""
    migrate(BEFORE)
    try:
        yield
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


def stamps():
    """Every job's stamp beside its live values, soft-deleted rows included."""
    return {
        job.pk: job
        for job in Job.all_objects.only(
            "plan_occurrence",
            "price_cents",
            "scheduled_end",
            "notes",
            "planned_price_cents",
            "planned_end",
            "planned_notes",
            "deleted_at",
        )
    }


@pytest.mark.django_db(transaction=True)
def test_the_backfill_stamps_every_plan_visit_and_no_other(request):
    organization = OrganizationFactory()
    plan = RecurringPlanFactory(
        organization=organization,
        rrule="FREQ=DAILY",
        starts_on=organization.today(),
        notes="Ring the bell.",
    )
    quiet_plan = RecurringPlanFactory(organization=organization, rrule="FREQ=DAILY", notes="")
    materialize_plan(plan)
    materialize_plan(quiet_plan)
    one_off = JobFactory(organization=organization, notes="Booked by phone.")

    plan_visits = list(Job.objects.filter(plan=plan).order_by("scheduled_start"))
    deleted, repriced = plan_visits[3], plan_visits[4]
    deleted.delete()  # soft
    # Edited before the migration, underneath the API: the documented case.
    Job.objects.filter(pk=repriced.pk).update(price_cents=4242, notes="By hand.")
    expected = Job.all_objects.filter(plan_occurrence__isnull=False).count()
    assert expected > 100

    request.getfixturevalue("rewound")
    with connection.cursor() as cursor:
        columns = {
            column.name
            for column in connection.introspection.get_table_description(cursor, "scheduling_job")
        }
    assert "planned_end" not in columns  # the rows really did lose their stamps

    migrate(AFTER)
    rows = stamps()

    planned = [job for job in rows.values() if job.plan_occurrence is not None]
    assert len(planned) == expected
    for job in planned:
        assert job.planned_price_cents == job.price_cents
        assert job.planned_end == job.scheduled_end
        assert job.planned_notes == job.notes
        assert job.planned_notes is not None

    # A soft-deleted plan visit is stamped like any other.
    assert rows[deleted.pk].deleted_at is not None
    assert rows[deleted.pk].planned_end == rows[deleted.pk].scheduled_end

    # An empty note is stamped as empty, not left missing.
    assert {
        rows[pk].planned_notes
        for pk in Job.objects.filter(plan=quiet_plan).values_list("pk", flat=True)
    } == {""}

    # The one case it gets wrong, and which way: a visit edited before the
    # migration is stamped with the edit, so it reads as untouched.
    assert rows[repriced.pk].planned_price_cents == 4242
    assert rows[repriced.pk].planned_notes == "By hand."

    # No plan wrote the one-off, and nothing about it changed.
    assert rows[one_off.pk].planned_price_cents is None
    assert rows[one_off.pk].planned_end is None
    assert rows[one_off.pk].planned_notes is None
    assert rows[one_off.pk].notes == "Booked by phone."
    assert rows[one_off.pk].price_cents == one_off.price_cents


@pytest.mark.django_db(transaction=True)
def test_backfilled_visits_regenerate_like_freshly_stamped_ones(request):
    """The point of the backfill: eight weeks of existing visits are not pinned."""
    from scheduling.services import regenerate_plan

    organization = OrganizationFactory()
    plan = RecurringPlanFactory(
        organization=organization, rrule="FREQ=DAILY", starts_on=organization.today()
    )
    materialize_plan(plan)

    request.getfixturevalue("rewound")
    migrate(AFTER)

    plan.preferred_start_time = dt.time(14, 0)
    plan.save()
    result = regenerate_plan(plan)

    assert result["kept"] == 0
    assert result["regenerated"] > 50
