"""
The beat entry, the fan-out and the per-organization task.

The task reads the clock itself, so these tests pin it with monkeypatch
rather than passing `now`.
"""

import ast
from pathlib import Path

import pytest
from django.conf import settings
from django.core import mail
from django.utils import timezone

from notifications import tasks
from notifications.models import VisitReminder
from notifications.tests.clock import NOW, TOMORROW_2PM
from scheduling.tests.factories import OrganizationFactory


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setattr(timezone, "now", lambda: NOW)


@pytest.fixture
def queued(monkeypatch):
    """The organization ids the fan-out hands to the per-organization task."""
    calls = []
    monkeypatch.setattr(tasks.send_visit_reminders, "delay", calls.append)
    return calls


def test_beat_runs_the_fan_out():
    """DatabaseScheduler syncs this dict; a task missing from it never runs."""
    entries = [
        entry
        for entry in settings.CELERY_BEAT_SCHEDULE.values()
        if entry["task"] == "notifications.send_visit_reminders_all"
    ]

    assert len(entries) == 1
    # Every hour, so each organization is looked at from its own clock.
    assert entries[0]["schedule"].hour == set(range(24))


@pytest.mark.django_db
class TestFanOut:
    def test_it_queues_each_organization_that_sends_by_its_own_id(self, org, queued):
        second = OrganizationFactory(visit_reminders_enabled=True)

        assert tasks.send_visit_reminders_all() == 2
        assert sorted(queued) == sorted([str(org.id), str(second.id)])

    def test_it_skips_organizations_that_have_not_turned_them_on(self, org, queued):
        OrganizationFactory(visit_reminders_enabled=False)

        tasks.send_visit_reminders_all()

        assert queued == [str(org.id)]

    def test_it_skips_a_deactivated_organization(self, org, queued):
        org.is_active = False
        org.save(update_fields=["is_active", "updated_at"])

        assert tasks.send_visit_reminders_all() == 0
        assert queued == []


@pytest.mark.django_db
class TestPerOrganization:
    def test_it_sends_what_is_due_now(self, org, make_visit, frozen):
        visit = make_visit(org, TOMORROW_2PM)

        assert tasks.send_visit_reminders(str(org.id)) == 1
        assert mail.outbox[0].to == [visit.customer.email]

    def test_run_twice_it_sends_once(self, org, make_visit, frozen):
        make_visit(org, TOMORROW_2PM)

        tasks.send_visit_reminders(str(org.id))
        tasks.send_visit_reminders(str(org.id))

        assert len(mail.outbox) == 1

    def test_one_organizations_run_never_touches_anothers_visits(self, org, make_visit, frozen):
        rival = OrganizationFactory(timezone="America/Denver", visit_reminders_enabled=True)
        ours = make_visit(org, TOMORROW_2PM)
        theirs = make_visit(rival, TOMORROW_2PM)

        assert tasks.send_visit_reminders(str(org.id)) == 1

        assert [message.to for message in mail.outbox] == [[ours.customer.email]]
        assert list(VisitReminder.objects.values_list("job_id", "organization_id")) == [
            (ours.id, org.id)
        ]

        assert tasks.send_visit_reminders(str(rival.id)) == 1
        assert mail.outbox[1].to == [theirs.customer.email]

    def test_an_organization_switched_off_after_the_fan_out_sends_nothing(
        self, org, make_visit, frozen
    ):
        make_visit(org, TOMORROW_2PM)
        org.visit_reminders_enabled = False
        org.save(update_fields=["visit_reminders_enabled", "updated_at"])

        assert tasks.send_visit_reminders(str(org.id)) == 0
        assert mail.outbox == []

    def test_an_organization_deactivated_after_the_fan_out_sends_nothing(
        self, org, make_visit, frozen
    ):
        make_visit(org, TOMORROW_2PM)
        org.is_active = False
        org.save(update_fields=["is_active", "updated_at"])

        assert tasks.send_visit_reminders(str(org.id)) == 0
        assert mail.outbox == []

    def test_an_unknown_organization_sends_nothing(self, db, frozen):
        assert tasks.send_visit_reminders("00000000-0000-0000-0000-000000000000") == 0


def test_nothing_the_reminders_read_imports_them():
    """
    The dependency runs one way: notifications reads scheduling, customers
    and organizations, and none of them knows it exists (ADR-035).
    """
    app_dir = Path(settings.BASE_DIR)
    offenders = []
    for app in ("scheduling", "customers", "organizations", "catalog", "billing"):
        for path in (app_dir / app).rglob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                if any(n == "notifications" or n.startswith("notifications.") for n in names):
                    offenders.append(str(path.relative_to(app_dir)))

    assert offenders == []
