"""Fixtures for the reminder tests. The instants they use are in `clock.py`."""

import datetime as dt

import pytest

from scheduling.tests.factories import JobFactory, OrganizationFactory


@pytest.fixture
def org(db):
    return OrganizationFactory(
        name="Sparkle Clean",
        timezone="America/Denver",
        visit_reminders_enabled=True,
        email="office@sparkle.example",
        phone="303-555-0100",
    )


@pytest.fixture
def make_visit(db):
    """A two-hour visit at `start` in `organization`; extra kwargs go to the factory."""

    def _make(organization, start, **kwargs):
        return JobFactory(
            organization=organization,
            scheduled_start=start,
            scheduled_end=start + dt.timedelta(hours=2),
            **kwargs,
        )

    return _make
