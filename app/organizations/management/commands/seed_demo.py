"""
Create two organizations with a full cast of users in each.

Two, not one, on purpose: tenant isolation bugs are invisible with a single
tenant in the database.

    docker compose run --rm web python manage.py seed_demo
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from organizations.models import Organization
from users.enums import Role
from users.models import CustomUser, Membership

DEMO_PASSWORD = "demo-password-change-me"

ORGANIZATIONS = [
    ("Sparkle Clean", "America/Denver"),
    ("Rival Cleaners", "America/New_York"),
]

ROLES = [Role.OWNER, Role.ADMIN, Role.DISPATCHER, Role.CLEANER, Role.CUSTOMER]


class Command(BaseCommand):
    help = "Seed two demo organizations with users in every role."

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
            organization, created = Organization.objects.get_or_create(
                name=name, defaults={"timezone": tz}
            )
            self.stdout.write(
                self.style.SUCCESS(f"{'Created' if created else 'Found'} {organization.name}")
            )

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
                self.stdout.write(f"  {role:11} {email}")

        self.stdout.write("")
        self.stdout.write(self.style.WARNING(f"All demo passwords: {DEMO_PASSWORD}"))
