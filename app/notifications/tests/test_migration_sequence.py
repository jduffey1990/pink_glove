"""
The numbering step in `0002_reminder_sequence`, run for real.

Rows written under 0001 had no `sequence`, and a visit reminded, moved and
reminded again had two of them. Going forward they must come out numbered
by age per visit, or the new unique constraint refuses to be created.

Transactional, because a migration cannot run inside the test's own
transaction; always finishes at the newest state, pass or fail.
"""

import datetime as dt

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from notifications.models import VisitReminder
from notifications.tests.clock import TOMORROW_2PM
from scheduling.tests.factories import JobFactory, OrganizationFactory

BEFORE = [("notifications", "0001_initial")]


@pytest.fixture
def rewound():
    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    try:
        yield MigrationExecutor(connection).loader.project_state(BEFORE).apps
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


@pytest.mark.django_db(transaction=True)
def test_existing_reminders_are_numbered_per_visit_by_age(rewound):
    organization = OrganizationFactory()
    moved = JobFactory(organization=organization, scheduled_start=TOMORROW_2PM)
    once = JobFactory(organization=organization, scheduled_start=TOMORROW_2PM)

    OldReminder = rewound.get_model("notifications", "VisitReminder")
    for job, start in [
        (moved, TOMORROW_2PM),
        (once, TOMORROW_2PM),
        (moved, TOMORROW_2PM + dt.timedelta(hours=2)),
    ]:
        OldReminder.objects.create(
            organization_id=organization.id,
            job_id=job.id,
            scheduled_start=start,
            recipient="dana@example.com",
        )

    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())

    assert list(
        VisitReminder.objects.filter(job=moved)
        .order_by("sequence")
        .values_list("sequence", "scheduled_start")
    ) == [(1, TOMORROW_2PM), (2, TOMORROW_2PM + dt.timedelta(hours=2))]
    assert VisitReminder.objects.get(job=once).sequence == 1
