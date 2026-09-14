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

from catalog.enums import PricingModel
from catalog.models import Service
from customers.models import Customer, ServiceLocation
from organizations.models import Organization
from scheduling.models import RecurringPlan
from scheduling.services import materialize_plan
from scheduling.tasks import materialize_organization
from users.enums import Role
from users.models import CustomUser, Membership

DEMO_PASSWORD = "demo-password-change-me"

ORGANIZATIONS = [
    ("Sparkle Clean", "America/Denver"),
    ("Rival Cleaners", "America/New_York"),
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

        for name, tz in ORGANIZATIONS:
            organization = self._organization(name, tz)
            users = self._users(organization)
            customers = self._customers(organization)
            services = self._services(organization)
            plans = self._plans(organization, customers, services, cleaner=users[Role.CLEANER])

            created = self._materialize(organization, plans)
            self.stdout.write(f"  materialized {created} jobs")

        self.stdout.write("")
        self.stdout.write(self.style.WARNING(f"All demo passwords: {DEMO_PASSWORD}"))

    # -- organization and people --------------------------------------------

    def _organization(self, name, tz) -> Organization:
        organization, created = Organization.objects.get_or_create(
            name=name, defaults={"timezone": tz}
        )
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
        today = timezone.now().astimezone(organization.tz).date()
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
