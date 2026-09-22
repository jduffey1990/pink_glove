"""
Shared fixtures for the billing tests.

An invoice needs an organization, a customer, a service and at least one
finished visit that all agree about the tenant. Three test modules needed the
same four rows and built them three times; they live here now, as
`scheduling/tests/conftest.py` already does for the schedule.

`completed_job` is a function rather than a fixture because most tests need
several visits on different days, which a fixture cannot give them.
"""

import datetime as dt
from decimal import Decimal

import pytest
from django.utils import timezone

from billing.tests.stripe_fixtures import ACCOUNT_ID as STRIPE_ACCOUNT_ID
from billing.tests.stripe_fixtures import WEBHOOK_SECRET
from scheduling.enums import JobStatus
from scheduling.tests.factories import (
    CustomerFactory,
    JobFactory,
    MembershipFactory,
    OrganizationFactory,
    ServiceFactory,
    UserFactory,
)
from users.enums import Role

# --- Stripe Connect (Phase 4b) -----------------------------------------------
# Not autouse: only the Stripe modules opt in, with
# `pytest.mark.usefixtures("stripe_on")`, so the 4a tests still run the
# product as it is without a key.


@pytest.fixture
def stripe_on(settings):
    """Stripe enabled on the server, with no network behind it."""
    settings.STRIPE_ENABLED = True
    settings.STRIPE_SECRET_KEY = "sk_test_x"
    settings.STRIPE_CONNECT_WEBHOOK_SECRET = WEBHOOK_SECRET
    settings.STRIPE_APPLICATION_FEE_PERCENT = Decimal("0")
    settings.FRONTEND_BASE_URL = "https://app.example.test"


@pytest.fixture
def connected(organization):
    """The root organization, connected to Stripe and taking cards."""
    organization.stripe_account_id = STRIPE_ACCOUNT_ID
    organization.stripe_charges_enabled = True
    organization.stripe_details_submitted = True
    organization.save()
    return organization


@pytest.fixture
def org(db):
    """
    The tenant under test.

    Denver rather than UTC on purpose: "the organization's today" is a
    different date from the server's for several hours a day, and an invoice
    is dated by the former.
    """
    # The name comes from the factory's sequence rather than a literal:
    # `Organization.name` is unique, and a fixed one collides with the root
    # conftest's `organization` the moment a test uses both.
    return OrganizationFactory(
        timezone="America/Denver",
        invoice_footer="Make checks payable to us. Thank you!",
    )


@pytest.fixture
def customer(org):
    return CustomerFactory(
        organization=org,
        first_name="Dana",
        last_name="Henderson",
        email="dana@example.com",
        billing_line1="14 Oak Street",
        billing_city="Denver",
        billing_state="CO",
        billing_postal_code="80202",
    )


@pytest.fixture
def service(org):
    return ServiceFactory(organization=org, name="Standard clean", base_price_cents=15000)


def completed_job(
    org,
    customer,
    service=None,
    *,
    status=JobStatus.COMPLETE,
    price_cents=15000,
    days_ago=1,
):
    """One finished visit, in the past, ready to be billed."""
    start = timezone.now() - dt.timedelta(days=days_ago)
    return JobFactory(
        organization=org,
        customer=customer,
        service=service or ServiceFactory(organization=org),
        status=status,
        price_cents=price_cents,
        scheduled_start=start,
        scheduled_end=start + dt.timedelta(hours=2),
    )


@pytest.fixture
def dispatcher(org):
    return staff_in(org, Role.DISPATCHER)


def staff_in(organization, role):
    user = UserFactory()
    MembershipFactory(user=user, organization=organization, role=role)
    return user


@pytest.fixture
def client_as(api_client):
    """Sign in as a given user. A real session, never force_authenticate."""

    def _sign_in(user):
        api_client.force_login(user)
        return api_client

    return _sign_in


@pytest.fixture
def dispatcher_client(client_as, dispatcher):
    return client_as(dispatcher)
