"""
Create two demo organizations with a full cast of users and a working schedule.

Two, not one, on purpose: tenant isolation bugs are invisible with a single
tenant in the database. Everything below is created per-organization, so any
query that forgets its tenant filter shows up as a wrong count immediately.

    docker compose run --rm web python manage.py seed_demo
"""

import datetime as dt
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from billing import services as billing
from billing.enums import PaymentMethod
from billing.models import Invoice
from catalog.enums import PricingModel
from catalog.models import Service
from customers.models import Customer, ServiceLocation
from organizations.enums import NoAccessFeeType
from organizations.models import Organization
from scheduling.enums import JobStatus
from scheduling.models import Job, RecurringPlan, TimeEntry
from scheduling.services import (
    activate_booked_customer,
    materialize_plan,
    quote_job_price,
    transition_job,
)
from scheduling.tasks import materialize_organization
from users.enums import Role
from users.models import CustomUser, Membership

DEMO_PASSWORD = "demo-password-change-me"

#: Deliberately different billing settings per tenant. One charges sales tax
#: and a flat no-access fee; the other charges no tax and a percentage of the
#: visit. A rule read from the wrong organization then shows up as a wrong
#: number rather than as nothing at all.
ORGANIZATIONS = [
    {
        "name": "Sparkle Clean",
        "timezone": "America/Denver",
        "tax_rate_percent": Decimal("8.250"),
        "no_access_fee_type": NoAccessFeeType.FLAT,
        "no_access_fee_value": Decimal("2500"),
        "invoice_prefix": "SPK",
        "invoice_terms_days": 14,
        "invoice_footer": "Make checks payable to Sparkle Clean. Thank you!",
        # On for one tenant only (ADR-035), so a reminder sent for the wrong
        # organization shows up in the worker's console. The seeded customers'
        # addresses are on the reserved `.test` domain, which mail never reaches.
        "visit_reminders_enabled": True,
    },
    {
        "name": "Rival Cleaners",
        "timezone": "America/New_York",
        "tax_rate_percent": Decimal("0.000"),
        "no_access_fee_type": NoAccessFeeType.PERCENT,
        "no_access_fee_value": Decimal("50"),
        "invoice_prefix": "RIV",
        "invoice_terms_days": 30,
        "invoice_footer": "Zelle to billing@rivalcleaners.test.",
        "visit_reminders_enabled": False,
    },
]

#: The two differ on the platform subscription too (Phase 4c): one is comped
#: and never shows a banner, the other is on a trial with five days left, so
#: the banner shows with a date. A rule read from the wrong tenant shows up
#: as the wrong banner. No Stripe objects are seeded, for 4b's reason -- a
#: fake customer id would make the settings card lie.
BILLING = {
    "Sparkle Clean": {"billing_exempt": True, "trial_days_left": None},
    "Rival Cleaners": {"billing_exempt": False, "trial_days_left": 5},
}

ROLES = [Role.OWNER, Role.ADMIN, Role.DISPATCHER, Role.CLEANER, Role.CUSTOMER]

#: Which customer each tenant's customer-role login is, by name from
#: `CUSTOMERS`. Different on purpose, so both portal homes can be shown:
#: Sparkle's is Dana, on the weekly plan, so `/my-visits` lists upcoming
#: visits; Rival's is Marcus, the one customer with nothing booked, so the
#: empty home is still there to see (ADR-039).
PORTAL_CUSTOMER = {
    "Sparkle Clean": ("Dana", "Whitfield"),
    "Rival Cleaners": ("Marcus", "Alvarez"),
}

#: One customer per interesting shape, because each exercises a different path:
#: square footage drives per-sqft pricing, pets and codes drive what a cleaner
#: sees on the job screen, and the reveal is only interesting where codes exist.
CUSTOMERS = [
    {
        "first_name": "Dana",
        "last_name": "Whitfield",
        "label": "Home",
        "line1": "1180 Marion Street",
        "square_feet": 2400,
        "has_pets": False,
        "pet_notes": "",
        "gate_code": "",
        "alarm_code": "",
    },
    {
        "first_name": "Marcus",
        "last_name": "Alvarez",
        "label": "Townhouse",
        "line1": "455 Pearl Street",
        "square_feet": None,
        "has_pets": True,
        "pet_notes": "Two cats, both hide. Do not let them out the front.",
        "gate_code": "",
        "alarm_code": "",
    },
    {
        "first_name": "Priya",
        "last_name": "Raman",
        "label": "Rental - Oak St",
        "line1": "82 Oak Street",
        "square_feet": 1650,
        "has_pets": False,
        "pet_notes": "",
        "gate_code": "4821#",
        "alarm_code": "9930",
    },
]

#: One per pricing model, so quoting is exercised end to end.
SERVICES = [
    {
        "name": "Standard Clean",
        "pricing_model": PricingModel.FLAT,
        "base_price_cents": 15000,
        "default_duration_minutes": 120,
    },
    {
        "name": "Deep Clean",
        "pricing_model": PricingModel.HOURLY,
        "base_price_cents": 20000,
        "hourly_rate_cents": Decimal("8500.00"),
        "default_duration_minutes": 240,
    },
    {
        "name": "Move-Out Clean",
        "pricing_model": PricingModel.PER_SQFT,
        "base_price_cents": 25000,
        "per_sqft_rate_cents": Decimal("18.000"),
        "default_duration_minutes": 300,
        # The one taxable service, so an organization with a rate has
        # something to apply it to and one without still reads correctly.
        "is_taxable": True,
    },
]


#: Rival's standard clean is a shorter, cheaper visit than Sparkle's, so every
#: visit line and every total built from one differs between the two tenants
#: before tax and the no-access fee do, and so do the hours on the timesheet.
SERVICE_OVERRIDES = {
    "Rival Cleaners": {
        "Standard Clean": {"base_price_cents": 13500, "default_duration_minutes": 90},
    },
}

#: The invoices each tenant gets, in the order they are drafted, as (state,
#: which visits). `_invoice_jobs` resolves the second against what is still
#: unbilled at that moment: "dana" is Dana's two oldest finished visits,
#: "priya" is Priya's oldest plan visit with the move-out clean at the same
#: rental (the seed's one taxable line), and "locked-out" is the no-access
#: visit, which bills as the organization's fee.
#:
#: Different on purpose. Sparkle has its no-access fee issued and not yet due;
#: Rival leaves its fee on a draft. So the tenants differ in invoice count as
#: well as in every amount, and a receivables or collections figure read from
#: the wrong tenant shows up as a wrong number.
INVOICES = {
    "Sparkle Clean": [
        ("paid", "dana"),
        ("overdue", "priya"),
        ("part-paid", "dana"),
        ("open", "locked-out"),
        ("draft", "dana"),
    ],
    "Rival Cleaners": [
        ("paid", "dana"),
        ("overdue", "priya"),
        ("part-paid", "dana"),
        ("draft", "locked-out"),
    ],
}


class Command(BaseCommand):
    help = "Seed two demo organizations with users, customers, services, plans and jobs."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Allow seeding even when organizations already exist.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        from django.conf import settings

        if not settings.DEBUG and not options["force"]:
            raise CommandError("Refusing to seed with DEBUG off. Pass --force if you mean it.")

        if Organization.objects.exists() and not options["force"]:
            raise CommandError("Organizations already exist. Pass --force to seed anyway.")

        for spec in ORGANIZATIONS:
            organization = self._organization(spec)
            users = self._users(organization)
            customers = self._customers(organization)
            self._portal_login(organization, users[Role.CUSTOMER], customers)
            services = self._services(organization)
            plans = self._plans(organization, customers, services, cleaner=users[Role.CLEANER])
            self._move_out(organization, customers[2], services[2], plans[0].starts_on)

            created = self._materialize(organization, plans)
            self.stdout.write(f"  materialized {created} jobs")

            self._close_the_past(organization, actor=users[Role.OWNER])
            self._record_time(organization)
            self._billing(organization, customers, actor=users[Role.DISPATCHER])

        self.stdout.write("")
        self.stdout.write(self.style.WARNING(f"All demo passwords: {DEMO_PASSWORD}"))

    # -- organization and people --------------------------------------------

    def _organization(self, spec) -> Organization:
        """
        The demo tenant, with its settings applied whether or not it is new.

        Everything else here is `get_or_create` and leaves an existing row
        alone, but the settings are different: a seed run against a database
        from before Phase 4 would otherwise build invoices at 0% tax with an
        INV prefix and claim to have demonstrated per-tenant billing. These
        two organizations exist to be overwritten.
        """
        settings_fields = {key: value for key, value in spec.items() if key != "name"}
        billing = BILLING[spec["name"]]
        settings_fields["billing_exempt"] = billing["billing_exempt"]
        settings_fields["trial_ends_at"] = (
            None
            if billing["trial_days_left"] is None
            else timezone.now() + dt.timedelta(days=billing["trial_days_left"])
        )
        organization, created = Organization.objects.get_or_create(
            name=spec["name"], defaults=settings_fields
        )

        if not created:
            for field, value in settings_fields.items():
                setattr(organization, field, value)
            organization.save(update_fields=[*settings_fields, "updated_at"])

        self.stdout.write(
            self.style.SUCCESS(f"{'Created' if created else 'Found'} {organization.name}")
        )
        return organization

    def _users(self, organization) -> dict:
        users = {}
        for role in ROLES:
            email = f"{role}@{organization.slug}.test"
            user = CustomUser.objects.filter(email=email).first()
            if user is None:
                user = CustomUser.objects.create_user(
                    email=email,
                    password=DEMO_PASSWORD,
                    first_name=role.label,
                    last_name=organization.name.split()[0],
                )
            Membership.objects.get_or_create(
                user=user, organization=organization, defaults={"role": role}
            )
            users[role] = user
            self.stdout.write(f"  {role:11} {email}")
        return users

    def _portal_login(self, organization, user, customers) -> None:
        """
        Link the tenant's customer-role login to its `PORTAL_CUSTOMER`.

        Without it a demo customer signs in to an empty home whoever they are,
        because a customer's visits are found through `Customer.user`.

        Held to the rule `CustomerSerializer.get_fields` puts on the API's
        link: the user is an active, customer-role member of this same
        organization. A database where that no longer holds, or where either
        side is already linked to someone else, is left as it is and said so:
        the link is one-to-one, and undoing a link someone made is not a
        seed's call. Additive, so a database seeded before this gains its
        links on a `--force` run.
        """
        name = PORTAL_CUSTOMER[organization.name]
        customer = next(c for c in customers if (c.first_name, c.last_name) == name)
        if customer.user_id == user.pk:
            return

        is_customer_member = Membership.objects.filter(
            user=user,
            organization=organization,
            role=Role.CUSTOMER,
            is_active=True,
        ).exists()
        if not is_customer_member:
            self.stdout.write(f"  {user.email} is not a customer here, left unlinked")
            return
        if customer.user_id is not None or Customer.all_objects.filter(user=user).exists():
            self.stdout.write(f"  {customer} or {user.email} is already linked, left as is")
            return

        customer.user = user
        customer.save(update_fields=["user", "updated_at"])
        self.stdout.write(f"  {user.email} signs in as {customer}")

    def _materialize(self, organization, plans) -> int:
        """
        Fill the board, backwards as well as forwards.

        The nightly task only ever creates jobs from today onward -- it has no
        business inventing history. A demo does: a schedule with nothing behind
        today reads as broken. So each plan is materialized once from its own
        start date, and then the ordinary task tops up the horizon.
        """
        created = 0
        for plan in plans:
            created += materialize_plan(plan, today=plan.starts_on)

        created += materialize_organization(str(organization.id))
        return created

    # -- book of business ----------------------------------------------------

    def _customers(self, organization) -> list[Customer]:
        customers = []
        for spec in CUSTOMERS:
            customer, _ = Customer.objects.get_or_create(
                organization=organization,
                first_name=spec["first_name"],
                last_name=spec["last_name"],
                defaults={
                    "email": (
                        f"{spec['first_name'].lower()}.{spec['last_name'].lower()}"
                        f"@{organization.slug}.test"
                    ),
                    "phone": "555-0100",
                },
            )
            ServiceLocation.objects.get_or_create(
                organization=organization,
                customer=customer,
                label=spec["label"],
                defaults={
                    "line1": spec["line1"],
                    "city": "Denver",
                    "state": "CO",
                    "postal_code": "80218",
                    "square_feet": spec["square_feet"],
                    "has_pets": spec["has_pets"],
                    "pet_notes": spec["pet_notes"],
                    "gate_code": spec["gate_code"],
                    "alarm_code": spec["alarm_code"],
                    "access_notes": "Side gate, then the back door." if spec["gate_code"] else "",
                },
            )
            customers.append(customer)

        self.stdout.write(f"  {len(customers)} customers with a location each")
        return customers

    def _services(self, organization) -> list[Service]:
        services = []
        overrides = SERVICE_OVERRIDES.get(organization.name, {})
        for spec in SERVICES:
            defaults = {k: v for k, v in spec.items() if k != "name"}
            defaults.update(overrides.get(spec["name"], {}))
            service, _ = Service.objects.get_or_create(
                organization=organization, name=spec["name"], defaults=defaults
            )
            services.append(service)

        self.stdout.write(f"  {len(services)} services (one per pricing model)")
        return services

    def _plans(self, organization, customers, services, *, cleaner) -> list[RecurringPlan]:
        """
        Two standing appointments: one weekly, one fortnightly.

        The weekly one carries the cleaner as a default assignee, so the
        materialized jobs come out already crewed and the "My Day" screen has
        something in it without anyone clicking Assign.
        """
        today = organization.today()
        # Seven weeks back, so the seeded board has history as well as a
        # future, and the history is old enough to bill honestly: an invoice
        # is issued after the visits on it, and the overdue one must have been
        # issued more than Rival's thirty-day terms ago. Seven and not eight:
        # the first materialization reaches eight weeks past the start and the
        # nightly task fills only from today, so an older start would leave
        # the days between the two empty.
        monday = today - dt.timedelta(days=today.weekday() + 49)

        specs = [
            {
                "customer": customers[0],
                "service": services[0],
                "rrule": "FREQ=WEEKLY;BYDAY=TU",
                "preferred_start_time": dt.time(9, 0),
                "assignees": [cleaner],
                "notes": "Standing Tuesday visit.",
            },
            {
                "customer": customers[2],
                "service": services[0],
                "rrule": "FREQ=WEEKLY;INTERVAL=2;BYDAY=TH",
                "preferred_start_time": dt.time(13, 0),
                "assignees": [],
                "notes": "Fortnightly. Codes are on file -- use the reveal button.",
            },
        ]

        plans = []
        for spec in specs:
            location = spec["customer"].locations.first()
            plan, created = RecurringPlan.objects.get_or_create(
                organization=organization,
                customer=spec["customer"],
                location=location,
                service=spec["service"],
                rrule=spec["rrule"],
                defaults={
                    "starts_on": monday,
                    "preferred_start_time": spec["preferred_start_time"],
                    "duration_minutes": spec["service"].default_duration_minutes,
                    "notes": spec["notes"],
                },
            )
            if created and spec["assignees"]:
                plan.default_assignees.set(spec["assignees"])
            # An active plan is a booking, and the API's plan create makes its
            # customer Active through this same call (ADR-037). Every run, not
            # only on create, so a database seeded before this gets its booked
            # customers out of Lead on a `--force` run; the call only ever
            # moves a Lead or an Inactive customer.
            activate_booked_customer(spec["customer"])
            plans.append(plan)

        self.stdout.write(f"  {len(plans)} recurring plans (weekly + fortnightly)")
        return plans

    def _move_out(self, organization, customer, service, starts_on) -> None:
        """
        One one-off visit: a move-out clean at the rental, between tenants.

        The seed's one taxable line, so the organization that charges tax has
        an invoice that shows it. The Wednesday of the plans' second week, so
        it is finished history by the time `_close_the_past` runs and is never
        the most recent visit, which that pass locks out.

        Priced as the API prices a one-off (`quote_job_price` with no plan),
        and its customer activated as the API's job create does. Found by
        customer and service rather than by date, so a later `--force` run
        adds no second one.
        """
        exists = Job.objects.filter(
            organization=organization, customer=customer, service=service, plan__isnull=True
        ).exists()
        if not exists:
            location = customer.locations.first()
            day = starts_on + dt.timedelta(days=9)
            start = dt.datetime.combine(day, dt.time(10, 0), tzinfo=organization.tz)
            minutes = service.default_duration_minutes
            Job.objects.create(
                organization=organization,
                customer=customer,
                location=location,
                service=service,
                scheduled_start=start,
                scheduled_end=start + dt.timedelta(minutes=minutes),
                price_cents=quote_job_price(
                    plan=None, service=service, location=location, duration_minutes=minutes
                ),
                notes="Tenant moved out. Inside the oven and every cupboard.",
            )
            self.stdout.write("  1 one-off visit (move-out clean)")
        activate_booked_customer(customer)

    # -- history, and the money that follows from it -------------------------

    def _close_the_past(self, organization, *, actor) -> None:
        """
        Finish every visit that has already happened.

        The materializer has no business inventing history, so a seeded board
        has weeks of past visits all still "scheduled" -- which reads as
        a business that never turned up. Walking them through the real state
        machine rather than setting the column keeps the demo honest about
        what the transitions allow.

        The most recent one becomes NO_ACCESS instead, so the no-access fee has
        something to price and the invoice screens show a fee line.
        """
        past = list(
            Job.objects.filter(
                organization=organization,
                status=JobStatus.SCHEDULED,
                scheduled_end__lt=timezone.now(),
            ).order_by("scheduled_start")
        )
        if not past:
            return

        locked_out = past[-1]
        for job in past:
            transition_job(job=job, to_status=JobStatus.IN_PROGRESS, actor=actor)
            if job.pk == locked_out.pk:
                transition_job(
                    job=job,
                    to_status=JobStatus.NO_ACCESS,
                    actor=actor,
                    reason="Nobody home and the side gate was bolted.",
                )
            else:
                transition_job(job=job, to_status=JobStatus.COMPLETE, actor=actor)

        self.stdout.write(f"  closed {len(past)} past visits ({1} of them no-access)")

    def _record_time(self, organization) -> None:
        """
        Put hours on the finished visits that had a crew.

        Closing a visit through the state machine clocks nobody in, so a
        seeded board had weeks of completed work and a timesheet with
        nothing on it. Each assigned cleaner gets one entry per completed
        visit: on site five minutes after the slot began, away ten minutes
        before it ended, because a timesheet of exact two-hour blocks reads
        as invented.

        Written directly rather than through `clock_in`, which stamps the
        present and refuses a finished job: this is history. Additive, and
        its own pass rather than part of `_close_the_past`, so a database
        seeded before this existed gains its hours on a `--force` run. A
        visit that already has an entry for that person is left alone.
        """
        finished = Job.objects.filter(
            organization=organization,
            status=JobStatus.COMPLETE,
            scheduled_end__lt=timezone.now(),
        ).prefetch_related("assignments", "time_entries")

        recorded = 0
        for job in finished:
            already = {entry.user_id for entry in job.time_entries.all()}
            for assignment in job.assignments.all():
                if assignment.user_id in already:
                    continue
                TimeEntry.objects.create(
                    organization=organization,
                    job=job,
                    user_id=assignment.user_id,
                    clock_in=job.scheduled_start + dt.timedelta(minutes=5),
                    clock_out=job.scheduled_end - dt.timedelta(minutes=10),
                )
                recorded += 1

        self.stdout.write(f"  recorded time on {recorded} finished visits")

    def _billing(self, organization, customers, *, actor) -> None:
        """
        The invoices in `INVOICES`: paid, overdue, part-paid with a tip, a
        draft, and for Sparkle an issued no-access fee not yet due.

        Built from `billing.services`, not by writing rows, so the seed
        exercises the same numbering, snapshot and ledger rules the API does --
        and breaks loudly here if any of them changes.

        Every date follows from the visits, as it would for a dispatcher who
        invoices after the work: an invoice is issued the day after its last
        visit (or today, for one finished today), so it is never dated before
        the work it bills, and the terms put the due date after that. The
        overdue one is overdue because its visits are old enough, not because
        its issue date was pushed into the past on its own.

        Once only. An organization that has any invoice, deleted or not, gets
        no more: what was issued cannot be edited (ADR-026), and a second
        round drawn from whatever visits are still unbilled would double the
        demo's money on every `--force` run.
        """
        if Invoice.all_objects.filter(organization=organization).exists():
            self.stdout.write("  invoices already seeded, none added")
            return

        today = organization.today()
        made = []
        for state, selector in INVOICES[organization.name]:
            jobs = self._invoice_jobs(organization, customers, selector)
            if not jobs:
                made.append(f"no {state} ({selector} has nothing to bill)")
                continue

            invoice = billing.draft_invoice(customer=jobs[0].customer, jobs=jobs, actor=actor)
            if state == "draft":
                made.append(state)
                continue

            last_visit = max(job.scheduled_start for job in jobs).astimezone(organization.tz)
            issued_on = min(last_visit.date() + dt.timedelta(days=1), today)
            billing.issue_invoice(invoice, actor=actor, today=issued_on)

            if state == "paid":
                billing.record_payment(
                    invoice,
                    method=PaymentMethod.CHECK,
                    amount_cents=invoice.total_cents,
                    received_on=min(issued_on + dt.timedelta(days=5), today),
                    reference="Check 1047",
                    actor=actor,
                )
            elif state == "part-paid":
                billing.record_payment(
                    invoice,
                    method=PaymentMethod.CASH,
                    amount_cents=invoice.total_cents // 2,
                    tip_cents=2000,
                    received_on=min(issued_on + dt.timedelta(days=1), today),
                    reference="Left on the counter",
                    actor=actor,
                )

            made.append(f"{invoice.number} {state}")

        self.stdout.write(f"  invoices: {', '.join(made)}")

    def _invoice_jobs(self, organization, customers, selector) -> list[Job]:
        """
        The visits one `INVOICES` entry bills, from what is still unbilled.

        Read through `billing.billable_jobs`, the dispatcher's own "ready to
        invoice" list, so a visit already on an invoice is never offered twice.
        """
        dana, _, priya = customers
        billable = list(billing.billable_jobs(organization))
        finished = [job for job in billable if job.status == JobStatus.COMPLETE]

        if selector == "dana":
            return [job for job in finished if job.customer_id == dana.pk][:2]
        if selector == "priya":
            visits = [job for job in finished if job.customer_id == priya.pk]
            planned = [job for job in visits if job.plan_id is not None][:1]
            one_off = [job for job in visits if job.plan_id is None][:1]
            return planned + one_off
        if selector == "locked-out":
            return [job for job in billable if job.status == JobStatus.NO_ACCESS][:1]
        raise CommandError(f"Unknown invoice selector {selector!r}.")
