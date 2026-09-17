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
from catalog.enums import PricingModel
from catalog.models import Service
from customers.models import Customer, ServiceLocation
from organizations.enums import NoAccessFeeType
from organizations.models import Organization
from scheduling.enums import JobStatus
from scheduling.models import Job, RecurringPlan
from scheduling.services import materialize_plan, transition_job
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
    },
]

ROLES = [Role.OWNER, Role.ADMIN, Role.DISPATCHER, Role.CLEANER, Role.CUSTOMER]

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
            services = self._services(organization)
            plans = self._plans(organization, customers, services, cleaner=users[Role.CLEANER])

            created = self._materialize(organization, plans)
            self.stdout.write(f"  materialized {created} jobs")

            self._close_the_past(organization, actor=users[Role.OWNER])
            self._billing(organization, actor=users[Role.DISPATCHER])

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
        for spec in SERVICES:
            service, _ = Service.objects.get_or_create(
                organization=organization,
                name=spec["name"],
                defaults={k: v for k, v in spec.items() if k != "name"},
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
        # A fortnight back, so the seeded board has recent history as well as a
        # future. A calendar that starts empty behind today looks broken.
        monday = today - dt.timedelta(days=today.weekday() + 14)

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
            plans.append(plan)

        self.stdout.write(f"  {len(plans)} recurring plans (weekly + fortnightly)")
        return plans

    # -- history, and the money that follows from it -------------------------

    def _close_the_past(self, organization, *, actor) -> None:
        """
        Finish every visit that has already happened.

        The materializer has no business inventing history, so a seeded board
        has a fortnight of past visits all still "scheduled" -- which reads as
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

    def _billing(self, organization, *, actor) -> None:
        """
        Four invoices in the four states a dispatcher actually sees: paid,
        part-paid with a tip, overdue, and still a draft.

        Built from `billing.services`, not by writing rows, so the seed
        exercises the same numbering, snapshot and ledger rules the API does --
        and breaks loudly here if any of them changes.
        """
        today = organization.today()
        billable = list(billing.billable_jobs(organization))
        if not billable:
            self.stdout.write("  no billable visits, so no invoices")
            return

        # One job per invoice, oldest first, so each invoice is legible.
        plans = [
            ("paid", 0),
            ("part-paid", 1),
            ("overdue", 2),
            ("draft", 3),
        ]

        made = []
        for kind, index in plans:
            if index >= len(billable):
                break
            job = billable[index]
            invoice = billing.draft_invoice(customer=job.customer, jobs=[job], actor=actor)

            if kind == "draft":
                made.append(f"{kind}")
                continue

            issued_on = today - dt.timedelta(days=60 if kind == "overdue" else 3)
            billing.issue_invoice(invoice, actor=actor, today=issued_on)

            if kind == "paid":
                billing.record_payment(
                    invoice,
                    method=PaymentMethod.CHECK,
                    amount_cents=invoice.total_cents,
                    received_on=issued_on + dt.timedelta(days=2),
                    reference="Check 1047",
                    actor=actor,
                )
            elif kind == "part-paid":
                billing.record_payment(
                    invoice,
                    method=PaymentMethod.CASH,
                    amount_cents=invoice.total_cents // 2,
                    tip_cents=2000,
                    received_on=issued_on + dt.timedelta(days=1),
                    reference="Left on the counter",
                    actor=actor,
                )

            made.append(f"{invoice.number} {kind}")

        self.stdout.write(f"  invoices: {', '.join(made)}")
