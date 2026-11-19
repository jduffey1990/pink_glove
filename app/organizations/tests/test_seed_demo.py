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
from scheduling.models import Job, JobAssignment, RecurringPlan, TimeEntry
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

    def test_booked_customers_are_active_and_one_lead_is_left(self, seeded):
        """
        ADR-037: booking work makes a customer Active. Each tenant keeps
        exactly one customer with nothing booked, as a Lead, so the customers
        list still shows that state.
        """
        from customers.enums import CustomerStatus

        for organization in Organization.objects.all():
            customers = Customer.objects.filter(organization=organization)
            booked = customers.filter(jobs__isnull=False).distinct()

            assert booked.exists()
            assert set(booked.values_list("status", flat=True)) == {CustomerStatus.ACTIVE}

            leads = customers.filter(status=CustomerStatus.LEAD)
            assert leads.count() == 1
            assert not leads.filter(jobs__isnull=False).exists()
            assert not leads.filter(plans__isnull=False).exists()

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

    def test_every_plan_visit_is_stamped_with_what_the_plan_wrote(self, seeded):
        """
        The seed works its past by hand after materializing. If it ever built
        plan visits itself, they would have no stamp, and an unstamped visit
        is one no plan edit will rebuild (ADR-032).
        """
        plan_visits = Job.all_objects.filter(plan_occurrence__isnull=False)

        assert plan_visits.exists()
        assert not plan_visits.filter(planned_end__isnull=True).exists()
        assert not plan_visits.filter(planned_price_cents__isnull=True).exists()
        assert not plan_visits.filter(planned_notes__isnull=True).exists()

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

    def test_the_finished_crewed_visits_have_hours_on_them(self, seeded):
        """
        Without these the timesheet report opens empty on a seeded database,
        which reads as a report that does not work.
        """
        from scheduling.enums import JobStatus

        for organization in Organization.objects.all():
            cleaner = CustomUser.objects.get(email=f"cleaner@{organization.slug}.test")
            entries = TimeEntry.objects.filter(organization=organization)
            crewed_and_done = Job.objects.filter(
                organization=organization, status=JobStatus.COMPLETE, assignments__user=cleaner
            )

            assert entries.count() == crewed_and_done.count() >= 2
            for entry in entries.select_related("job"):
                assert entry.user_id == cleaner.pk
                assert entry.job.organization_id == organization.pk
                assert entry.job.scheduled_start < entry.clock_in < entry.clock_out
                assert entry.clock_out < entry.job.scheduled_end


@pytest.mark.django_db
class TestSeededPortalLogins:
    """
    Each tenant's customer login is linked to one of its customers, so a demo
    customer who signs in sees a home (ADR-039): Sparkle's with visits on it,
    Rival's the empty one.
    """

    def _linked(self, slug):
        user = CustomUser.objects.get(email=f"customer@{slug}.test")
        return user, Customer.objects.get(user=user)

    def test_sparkles_login_is_a_customer_with_upcoming_visits(self, seeded):
        from django.utils import timezone

        user, customer = self._linked("sparkle-clean")

        assert customer.organization.name == "Sparkle Clean"
        assert (customer.first_name, customer.last_name) == ("Dana", "Whitfield")
        assert Job.objects.filter(customer=customer).exists()
        assert Job.objects.filter(customer=customer, scheduled_start__gt=timezone.now()).exists()

    def test_rivals_login_is_the_lead_with_nothing_booked(self, seeded):
        from customers.enums import CustomerStatus

        user, customer = self._linked("rival-cleaners")

        assert customer.organization.name == "Rival Cleaners"
        assert (customer.first_name, customer.last_name) == ("Marcus", "Alvarez")
        assert customer.status == CustomerStatus.LEAD
        assert not Job.all_objects.filter(customer=customer).exists()

    def test_each_link_is_one_the_api_would_accept(self, seeded, rf):
        """`CustomerSerializer.get_fields` offers only active customer members."""
        from customers.serializers import CustomerSerializer

        for slug in ("sparkle-clean", "rival-cleaners"):
            user, customer = self._linked(slug)
            request = rf.get("/")
            request.organization = customer.organization
            fields = CustomerSerializer(context={"request": request}).get_fields()

            assert fields["user"].queryset.filter(pk=user.pk).exists()

    def test_only_the_two_logins_are_linked(self, seeded):
        assert Customer.all_objects.filter(user__isnull=False).count() == 2

    def test_the_sparkle_login_reads_its_own_visits_through_the_api(self, seeded):
        """What `/my-visits` asks for: the next eight weeks, Dana's and nobody else's."""
        import datetime as dt

        from rest_framework.test import APIClient

        user, customer = self._linked("sparkle-clean")
        today = customer.organization.today()
        client = APIClient()
        client.force_login(user)

        response = client.get(
            "/api/scheduling/jobs/",
            {
                "date_from": today.isoformat(),
                "date_to": (today + dt.timedelta(days=56)).isoformat(),
                "ordering": "scheduled_start",
            },
        )

        assert response.status_code == 200
        body = response.json()
        visits = body["results"] if isinstance(body, dict) else body
        assert visits
        assert {visit["customer"] for visit in visits} == {str(customer.pk)}

    def test_force_links_an_older_seeds_logins(self, seeded, settings):
        settings.DEBUG = True
        Customer.all_objects.update(user=None)

        call_command("seed_demo", "--force", verbosity=0)

        assert Customer.all_objects.filter(user__isnull=False).count() == 2

    def test_force_leaves_a_link_someone_made_alone(self, seeded, settings):
        """The link is one-to-one; the seed never takes it from another customer."""
        settings.DEBUG = True
        user, dana = self._linked("sparkle-clean")
        dana.user = None
        dana.save(update_fields=["user", "updated_at"])
        priya = Customer.objects.get(organization=dana.organization, first_name="Priya")
        priya.user = user
        priya.save(update_fields=["user", "updated_at"])

        call_command("seed_demo", "--force", verbosity=0)

        priya.refresh_from_db()
        dana.refresh_from_db()
        assert priya.user_id == user.pk
        assert dana.user_id is None


@pytest.mark.django_db
class TestSeedGuards:
    def test_it_refuses_to_run_twice_without_force(self, seeded, settings):
        settings.DEBUG = True

        with pytest.raises(CommandError):
            call_command("seed_demo", verbosity=0)

    def test_force_makes_it_idempotent_rather_than_duplicating(self, seeded, settings):
        """
        Invoices and payments too. A second round drawn from the visits still
        unbilled used to add two more issued invoices per tenant on every
        `--force` run, which doubled the demo's money.
        """
        from billing.models import Invoice, Payment

        settings.DEBUG = True

        def counts():
            return (
                Organization.objects.count(),
                Customer.objects.count(),
                Job.objects.count(),
                TimeEntry.objects.count(),
                Invoice.all_objects.count(),
                Payment.all_objects.count(),
            )

        before = counts()

        call_command("seed_demo", "--force", verbosity=0)

        assert counts() == before

    def test_force_moves_an_older_seeds_booked_customers_out_of_lead(self, seeded, settings):
        """
        A database seeded before ADR-037 has its booked customers as Leads.
        Running again activates them, through the same call a booking makes,
        and leaves the one with nothing booked alone.
        """
        from customers.enums import CustomerStatus

        settings.DEBUG = True
        Customer.objects.update(status=CustomerStatus.LEAD)

        call_command("seed_demo", "--force", verbosity=0)

        for organization in Organization.objects.all():
            statuses = Customer.objects.filter(organization=organization).values_list(
                "status", flat=True
            )
            assert sorted(statuses) == ["active", "active", "lead"]

    def test_force_gives_an_older_seed_its_hours(self, seeded, settings):
        """
        A database seeded before the seed recorded time has finished visits
        and no entries. Running again fills them in, and adds nothing else.
        """
        settings.DEBUG = True
        recorded = TimeEntry.objects.count()
        TimeEntry.all_objects.all().hard_delete()

        call_command("seed_demo", "--force", verbosity=0)

        assert TimeEntry.objects.count() == recorded > 0

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

        counts = {}
        for organization in Organization.objects.all():
            invoices = Invoice.objects.filter(organization=organization)
            statuses = set(invoices.values_list("status", flat=True))

            counts[organization.name] = invoices.count()
            assert {InvoiceStatus.DRAFT, InvoiceStatus.ISSUED} <= statuses

        # Different on purpose: Sparkle issues its no-access fee, Rival drafts it.
        assert counts == {"Sparkle Clean": 5, "Rival Cleaners": 4}

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

    def test_every_invoice_is_dated_after_the_work_it_bills(self, seeded):
        """
        Issued on or after its last visit, due after it was issued, paid
        between issue and today. The seed once issued its overdue invoice sixty
        days back for a visit three weeks old, so the pay page asked for money
        a month before the clean it was for.
        """
        from billing.enums import InvoiceStatus
        from billing.models import Invoice, Payment

        issued = Invoice.objects.filter(status=InvoiceStatus.ISSUED).select_related("organization")
        assert issued.exists()

        for invoice in issued:
            organization = invoice.organization
            visits = [
                line.job.scheduled_start.astimezone(organization.tz).date()
                for line in invoice.lines.select_related("job")
                if line.job_id
            ]
            assert visits, invoice.number
            assert invoice.issued_on >= max(visits), invoice.number
            assert invoice.due_on > invoice.issued_on, invoice.number
            assert invoice.issued_on <= organization.today(), invoice.number

        for payment in Payment.objects.select_related("invoice__organization"):
            invoice = payment.invoice
            assert invoice.issued_on <= payment.received_on <= invoice.organization.today()

    def test_one_invoice_per_tenant_is_overdue_on_its_dates(self, seeded):
        """
        Overdue because its due date is behind today, and its due date is
        behind today because its visits are old, not because the issue date
        was moved on its own. Rival's thirty-day terms are the hard case.
        """
        from billing import services as billing
        from billing.enums import InvoiceStatus
        from billing.models import Invoice

        for organization in Organization.objects.all():
            overdue = [
                invoice
                for invoice in Invoice.objects.filter(
                    organization=organization, status=InvoiceStatus.ISSUED
                )
                if billing.is_overdue(invoice)
            ]
            assert overdue, organization.name
            for invoice in overdue:
                assert invoice.due_on < organization.today()

    def test_the_taxed_tenant_charges_tax_and_the_other_does_not(self, seeded):
        """The move-out clean is the one taxable line; only Sparkle has a rate."""
        from billing.enums import InvoiceStatus
        from billing.models import Invoice

        issued = Invoice.objects.filter(status=InvoiceStatus.ISSUED)

        assert issued.filter(organization__name="Sparkle Clean", tax_cents__gt=0).exists()
        assert issued.filter(organization__name="Rival Cleaners").exists()
        assert not issued.filter(organization__name="Rival Cleaners", tax_cents__gt=0).exists()

    def test_each_no_access_fee_follows_its_own_tenants_rule(self, seeded):
        """Sparkle's is a flat $25; Rival's is half the visit it replaced."""
        from billing.enums import LineKind
        from billing.models import InvoiceLine

        sparkle = InvoiceLine.objects.get(
            organization__name="Sparkle Clean", kind=LineKind.NO_ACCESS_FEE
        )
        rival = InvoiceLine.objects.select_related("job").get(
            organization__name="Rival Cleaners", kind=LineKind.NO_ACCESS_FEE
        )

        assert sparkle.amount_cents == 2500
        assert rival.amount_cents == rival.job.price_cents // 2
        assert rival.amount_cents != sparkle.amount_cents

    def test_some_invoices_bill_more_than_one_visit(self, seeded):
        """One line per invoice never shows how lines add up."""
        from django.db.models import Count

        from billing.models import Invoice

        for organization in Organization.objects.all():
            multi = (
                Invoice.objects.filter(organization=organization)
                .annotate(n=Count("lines"))
                .filter(n__gt=1)
            )
            assert multi.exists(), organization.name


@pytest.mark.django_db
class TestSeededSubscription:
    """The two differ on purpose (Phase 4c): no banner here, a dated banner there."""

    def test_one_is_comped_and_the_other_on_a_short_trial(self, seeded, settings):
        import datetime as dt

        from django.utils import timezone

        from organizations import services

        settings.PLATFORM_BILLING_ENABLED = True
        sparkle = Organization.objects.get(name="Sparkle Clean")
        rival = Organization.objects.get(name="Rival Cleaners")

        assert sparkle.billing_exempt is True
        assert sparkle.trial_ends_at is None
        assert services.billing_notice(sparkle, Role.OWNER) is None

        assert rival.billing_exempt is False
        assert abs(rival.trial_ends_at - (timezone.now() + dt.timedelta(days=5))) < dt.timedelta(
            minutes=5
        )
        level, text = services.billing_notice(rival, Role.OWNER)
        assert level == "warning"
        assert text.startswith("Your free trial ends on")

    def test_nothing_in_stripe_is_seeded(self, seeded):
        for organization in Organization.objects.all():
            assert organization.stripe_customer_id == ""
            assert organization.stripe_subscription_id == ""
            assert organization.subscription_status == ""


@pytest.mark.django_db
class TestSeededReports:
    """
    The reports are the first thing an owner opens, and the seed is what
    they are first opened against. Each one has to have something in it, and
    the two tenants have to disagree, or the demo proves nothing about either.
    """

    def _reports(self, organization):
        import datetime as dt

        from django.urls import reverse
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_login(CustomUser.objects.get(email=f"owner@{organization.slug}.test"))

        today = organization.today()
        recent = {
            "date_from": (today - dt.timedelta(days=45)).isoformat(),
            "date_to": today.isoformat(),
        }
        responses = {
            "timesheet": client.get(reverse("scheduling:report-timesheet"), recent),
            "receivables": client.get(reverse("billing:report-receivables")),
            "collections": client.get(reverse("billing:report-collections"), recent),
        }
        assert {name: r.status_code for name, r in responses.items()} == dict.fromkeys(
            responses, 200
        )
        return {name: response.json() for name, response in responses.items()}

    def test_every_report_has_something_in_it(self, seeded):
        for organization in Organization.objects.all():
            reports = self._reports(organization)

            assert reports["timesheet"]["total_seconds"] > 0
            assert reports["timesheet"]["staff"][0]["name"].startswith("Cleaner")
            assert reports["receivables"]["totals"]["total_cents"] > 0
            assert reports["collections"]["totals"]["payment_count"] >= 2
            assert reports["collections"]["totals"]["tip_cents"] > 0

    def test_each_timesheet_names_its_own_cleaner_and_nobody_elses(self, seeded):
        for organization in Organization.objects.all():
            names = [person["name"] for person in self._reports(organization)["timesheet"]["staff"]]

            assert names == [f"Cleaner {organization.name.split()[0]}"]

    def test_the_two_tenants_disagree_on_every_total(self, seeded):
        """
        Owed, received and hours worked all differ between the tenants, so a
        report that read the other tenant's rows shows a wrong number rather
        than a right-looking one. They differ by tax, by the no-access fee
        rule, by the price and length of the standard clean, and by how many
        invoices were issued.
        """
        sparkle, rival = (
            self._reports(Organization.objects.get(name=name))
            for name in ("Sparkle Clean", "Rival Cleaners")
        )

        for report, total in (
            ("receivables", "total_cents"),
            ("receivables", "invoice_count"),
            ("collections", "amount_cents"),
            ("timesheet", "total_seconds"),
        ):
            mine = sparkle[report]
            theirs = rival[report]
            if report != "timesheet":
                mine, theirs = mine["totals"], theirs["totals"]
            assert mine[total] != theirs[total], (report, total)


@pytest.mark.django_db
class TestSeededReminders:
    def test_exactly_one_organization_sends_visit_reminders(self, seeded):
        """ADR-035: one on and one off, so a reminder for the wrong tenant is visible."""
        assert Organization.objects.get(name="Sparkle Clean").visit_reminders_enabled is True
        assert Organization.objects.get(name="Rival Cleaners").visit_reminders_enabled is False

    def test_every_seeded_customer_is_on_a_domain_mail_never_reaches(self, seeded):
        """A seeded reminder cannot reach a real inbox whatever backend is configured."""
        emails = Customer.objects.values_list("email", flat=True)

        assert emails
        assert all(email.endswith(".test") for email in emails)
