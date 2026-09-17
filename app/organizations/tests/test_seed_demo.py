"""
The demo seed.

It is the first thing anyone runs against this codebase, and the walkthrough in
PLAN.md's exit criteria depends on it producing a board with jobs on it. A seed
that half-works wastes the first hour of every new session.
"""

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from catalog.models import Service
from customers.models import Customer, ServiceLocation
from organizations.models import Organization
from scheduling.models import Job, JobAssignment, RecurringPlan
from users.enums import Role
from users.models import CustomUser, Membership


@pytest.fixture
def seeded(db, settings):
    settings.DEBUG = True
    call_command("seed_demo", verbosity=0)


@pytest.mark.django_db
class TestSeedShape:
    def test_it_creates_two_organizations(self, seeded):
        """Two, not one -- tenant isolation bugs are invisible with a single tenant."""
        assert Organization.objects.count() == 2

    def test_every_role_exists_in_every_organization(self, seeded):
        for organization in Organization.objects.all():
            roles = set(
                Membership.objects.filter(organization=organization).values_list("role", flat=True)
            )
            assert roles == set(Role.values)

    def test_each_organization_gets_its_own_book(self, seeded):
        for organization in Organization.objects.all():
            assert Customer.objects.filter(organization=organization).count() == 3
            assert ServiceLocation.objects.filter(organization=organization).count() == 3
            assert Service.objects.filter(organization=organization).count() == 3
            assert RecurringPlan.objects.filter(organization=organization).count() == 2

    def test_the_locations_cover_the_interesting_shapes(self, seeded):
        """Square footage, pets, and codes each drive a different code path."""
        for organization in Organization.objects.all():
            locations = ServiceLocation.objects.filter(organization=organization)

            assert locations.filter(square_feet__isnull=False).exists()
            assert locations.filter(has_pets=True).exists()
            assert any(loc.gate_code for loc in locations)

    def test_there_is_one_service_per_pricing_model(self, seeded):
        from catalog.enums import PricingModel

        for organization in Organization.objects.all():
            models = set(
                Service.objects.filter(organization=organization).values_list(
                    "pricing_model", flat=True
                )
            )
            assert models == set(PricingModel.values)


@pytest.mark.django_db
class TestSeededSchedule:
    def test_jobs_are_materialized(self, seeded):
        for organization in Organization.objects.all():
            assert Job.objects.filter(organization=organization).exists()

    def test_the_cleaner_is_on_the_weekly_plans_jobs(self, seeded):
        for organization in Organization.objects.all():
            cleaner = CustomUser.objects.get(email=f"cleaner@{organization.slug}.test")
            assert JobAssignment.objects.filter(organization=organization, user=cleaner).exists()

    def test_jobs_land_at_the_local_wall_clock_time(self, seeded):
        """
        The Denver and New York plans both say 9am. If they came out at the
        same UTC hour, expansion ignored the organization's timezone.
        """
        hours = set()
        for organization in Organization.objects.all():
            job = (
                Job.objects.filter(organization=organization, plan__preferred_start_time="09:00")
                .order_by("scheduled_start")
                .first()
            )
            assert job.scheduled_start.astimezone(organization.tz).hour == 9
            hours.add(job.scheduled_start.hour)

        assert len(hours) == 2

    def test_the_board_has_both_history_and_a_future(self, seeded):
        from django.utils import timezone

        now = timezone.now()
        for organization in Organization.objects.all():
            jobs = Job.objects.filter(organization=organization)
            assert jobs.filter(scheduled_start__lt=now).exists()
            assert jobs.filter(scheduled_start__gt=now).exists()

    def test_no_job_crosses_a_tenant_boundary(self, seeded):
        for job in Job.objects.all():
            assert job.customer.organization_id == job.organization_id
            assert job.location.organization_id == job.organization_id
            assert job.service.organization_id == job.organization_id


@pytest.mark.django_db
class TestSeedGuards:
    def test_it_refuses_to_run_twice_without_force(self, seeded, settings):
        settings.DEBUG = True

        with pytest.raises(CommandError):
            call_command("seed_demo", verbosity=0)

    def test_force_makes_it_idempotent_rather_than_duplicating(self, seeded, settings):
        settings.DEBUG = True
        before = (Organization.objects.count(), Job.objects.count())

        call_command("seed_demo", "--force", verbosity=0)

        assert (Organization.objects.count(), Job.objects.count()) == before

    def test_it_refuses_to_run_with_debug_off(self, db, settings):
        settings.DEBUG = False

        with pytest.raises(CommandError):
            call_command("seed_demo", verbosity=0)


@pytest.mark.django_db
class TestSeededBilling:
    """
    The seed builds invoices through `billing.services`, not by writing rows,
    so these assertions also stand as a check that numbering, the snapshot and
    the ledger still agree with each other.
    """

    def test_the_two_organizations_bill_differently(self, seeded):
        """A rule read from the wrong tenant shows up as a wrong number here."""
        from decimal import Decimal

        from organizations.enums import NoAccessFeeType

        sparkle = Organization.objects.get(name="Sparkle Clean")
        rival = Organization.objects.get(name="Rival Cleaners")

        assert sparkle.tax_rate_percent == Decimal("8.250")
        assert rival.tax_rate_percent == Decimal("0.000")
        assert sparkle.no_access_fee_type == NoAccessFeeType.FLAT
        assert rival.no_access_fee_type == NoAccessFeeType.PERCENT
        assert (sparkle.invoice_prefix, rival.invoice_prefix) == ("SPK", "RIV")

    def test_the_past_is_finished_and_one_visit_was_locked_out(self, seeded):
        from django.utils import timezone

        from scheduling.enums import JobStatus

        for organization in Organization.objects.all():
            past = Job.objects.filter(organization=organization, scheduled_end__lt=timezone.now())
            assert past.exists()
            assert not past.filter(status=JobStatus.SCHEDULED).exists()
            assert past.filter(status=JobStatus.NO_ACCESS).count() == 1

    def test_each_organization_has_invoices_in_several_states(self, seeded):
        """
        Both states, not either: a demo that seeds four drafts and no issued
        invoice passes an `or` and shows a dispatcher nothing worth seeing.
        """
        from billing.enums import InvoiceStatus
        from billing.models import Invoice

        for organization in Organization.objects.all():
            invoices = Invoice.objects.filter(organization=organization)
            statuses = set(invoices.values_list("status", flat=True))

            assert invoices.count() == 4
            assert {InvoiceStatus.DRAFT, InvoiceStatus.ISSUED} <= statuses

    def test_numbers_use_the_organizations_own_prefix_and_sequence(self, seeded):
        from billing.models import Invoice

        for organization in Organization.objects.all():
            numbers = sorted(
                Invoice.objects.filter(organization=organization)
                .exclude(number="")
                .values_list("number", flat=True)
            )
            assert numbers
            assert numbers[0] == f"{organization.invoice_prefix}-0001"

    def test_there_is_a_paid_one_a_part_paid_one_and_an_overdue_one(self, seeded):
        from billing import services as billing
        from billing.enums import InvoiceStatus, PaymentState
        from billing.models import Invoice

        for organization in Organization.objects.all():
            issued = Invoice.objects.filter(
                organization=organization, status=InvoiceStatus.ISSUED
            ).select_related("organization")
            states = {billing.payment_state(invoice) for invoice in issued}

            assert PaymentState.PAID in states
            assert PaymentState.PARTIAL in states
            assert any(billing.is_overdue(invoice) for invoice in issued)

    def test_the_part_paid_one_carries_a_tip_that_settles_nothing(self, seeded):
        from billing.models import Payment

        tipped = Payment.objects.filter(tip_cents__gt=0).select_related("invoice")

        assert tipped.exists()
        for payment in tipped:
            assert payment.amount_cents < payment.invoice.total_cents
