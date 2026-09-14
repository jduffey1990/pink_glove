"""
Fixtures for the scheduling API tests.

Each role gets a signed-in client, because almost every question in this
package is "what does *this* role see or get refused". Sign-in is always
`force_login` -- DRF's `force_authenticate` attaches the user after Django
middleware has run, so `TenantMiddleware` would see AnonymousUser and resolve
no organization, and the tests would pass or fail for the wrong reason.
"""

import datetime as dt

import pytest
from rest_framework.test import APIClient

from scheduling.tests.factories import (
    CustomerFactory,
    JobFactory,
    ServiceFactory,
    ServiceLocationFactory,
)
from users.enums import Role


@pytest.fixture
def make_client(db):
    def _make(user):
        client = APIClient()
        client.force_login(user)
        return client

    return _make


@pytest.fixture
def dispatcher(organization, make_member):
    return make_member(organization, role=Role.DISPATCHER, email="dispatcher@example.com")


@pytest.fixture
def cleaner(organization, make_member):
    return make_member(organization, role=Role.CLEANER, email="cleaner@example.com")


@pytest.fixture
def other_cleaner(organization, make_member):
    return make_member(organization, role=Role.CLEANER, email="cleaner2@example.com")


@pytest.fixture
def customer_user(organization, make_member):
    return make_member(organization, role=Role.CUSTOMER, email="customer@example.com")


@pytest.fixture
def rival_cleaner(other_organization, make_member):
    return make_member(other_organization, role=Role.CLEANER, email="rival@example.com")


@pytest.fixture
def dispatcher_client(dispatcher, make_client):
    return make_client(dispatcher)


@pytest.fixture
def cleaner_client(cleaner, make_client):
    return make_client(cleaner)


@pytest.fixture
def customer_client(customer_user, make_client):
    return make_client(customer_user)


@pytest.fixture
def service(organization):
    return ServiceFactory(organization=organization)


@pytest.fixture
def customer(organization, customer_user):
    """A customer record wired to a portal login, so customer-scope tests work."""
    return CustomerFactory(organization=organization, user=customer_user)


@pytest.fixture
def location(organization, customer):
    return ServiceLocationFactory(organization=organization, customer=customer)


@pytest.fixture
def job(organization, customer, location, service):
    """Tomorrow at this time, two hours."""
    return JobFactory(
        organization=organization, customer=customer, location=location, service=service
    )


@pytest.fixture
def assigned_job(job, cleaner, organization):
    """
    The same row as `job`, with the cleaner on it.

    A test that needs both "a job this cleaner is on" and "a job they are not"
    wants `assigned_job` and `other_job`, not `assigned_job` and `job` -- the
    latter two are one object and the test would quietly assert nothing.
    """
    from scheduling.models import JobAssignment

    JobAssignment.objects.create(organization=organization, job=job, user=cleaner)
    return job


@pytest.fixture
def other_job(organization, service):
    """A second job in the same organization that nobody is assigned to."""
    return JobFactory(organization=organization, service=service)


@pytest.fixture
def rival_job(other_organization):
    return JobFactory(organization=other_organization)


@pytest.fixture
def two_hours():
    return dt.timedelta(hours=2)
