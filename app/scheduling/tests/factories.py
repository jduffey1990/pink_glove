"""
factory-boy factories for the scheduling models.

A `Job` needs an organization, a customer, a location under that customer, and
a service -- four related rows that must all agree about the tenant, or
`TenantModel.save()` rejects them. Building that by hand in every test is how
the earlier phases' tests looked, and it does not scale past two FKs.

`organization` is a trait on every factory: pass it and the whole related graph
is built inside that organization.
"""

import datetime as dt

import factory
from django.utils import timezone

from catalog.enums import PricingModel
from users.enums import Role


class OrganizationFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = "organizations.Organization"

    name = factory.Sequence(lambda n: f"Cleaning Co {n}")
    timezone = "America/Denver"


class UserFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = "users.CustomUser"
        skip_postgeneration_save = True

    email = factory.Sequence(lambda n: f"user{n}@example.com")
    first_name = "Sam"
    last_name = factory.Sequence(lambda n: f"Cleaner{n}")

    @factory.post_generation
    def password(obj, create, extracted, **kwargs):
        if create:
            obj.set_password(extracted or "pw-for-tests-only")
            obj.save(update_fields=["password"])


class MembershipFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = "users.Membership"

    user = factory.SubFactory(UserFactory)
    organization = factory.SubFactory(OrganizationFactory)
    role = Role.CLEANER


class CustomerFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = "customers.Customer"

    organization = factory.SubFactory(OrganizationFactory)
    first_name = "Dana"
    last_name = factory.Sequence(lambda n: f"Henderson{n}")
    email = factory.Sequence(lambda n: f"customer{n}@example.com")


class ServiceLocationFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = "customers.ServiceLocation"

    # The customer inherits this factory's organization, so the two agree.
    organization = factory.SubFactory(OrganizationFactory)
    customer = factory.SubFactory(
        CustomerFactory, organization=factory.SelfAttribute("..organization")
    )
    label = "Home"
    line1 = factory.Sequence(lambda n: f"{n} Oak Street")
    city = "Denver"
    state = "CO"
    postal_code = "80202"
    square_feet = 2000


class ServiceFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = "catalog.Service"

    organization = factory.SubFactory(OrganizationFactory)
    name = factory.Sequence(lambda n: f"Standard Clean {n}")
    pricing_model = PricingModel.FLAT
    base_price_cents = 15000
    default_duration_minutes = 120


class RecurringPlanFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = "scheduling.RecurringPlan"
        skip_postgeneration_save = True

    organization = factory.SubFactory(OrganizationFactory)
    customer = factory.SubFactory(
        CustomerFactory, organization=factory.SelfAttribute("..organization")
    )
    location = factory.SubFactory(
        ServiceLocationFactory,
        organization=factory.SelfAttribute("..organization"),
        customer=factory.SelfAttribute("..customer"),
    )
    service = factory.SubFactory(
        ServiceFactory, organization=factory.SelfAttribute("..organization")
    )

    rrule = "FREQ=WEEKLY;BYDAY=TU"
    starts_on = factory.LazyFunction(lambda: timezone.now().date())
    preferred_start_time = dt.time(9, 0)
    duration_minutes = 120
    is_active = True

    @factory.post_generation
    def default_assignees(obj, create, extracted, **kwargs):
        if create and extracted:
            obj.default_assignees.set(extracted)


class JobFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = "scheduling.Job"

    organization = factory.SubFactory(OrganizationFactory)
    customer = factory.SubFactory(
        CustomerFactory, organization=factory.SelfAttribute("..organization")
    )
    location = factory.SubFactory(
        ServiceLocationFactory,
        organization=factory.SelfAttribute("..organization"),
        customer=factory.SelfAttribute("..customer"),
    )
    service = factory.SubFactory(
        ServiceFactory, organization=factory.SelfAttribute("..organization")
    )

    scheduled_start = factory.LazyFunction(
        lambda: timezone.now().replace(microsecond=0) + dt.timedelta(days=1)
    )
    scheduled_end = factory.LazyAttribute(lambda o: o.scheduled_start + dt.timedelta(minutes=120))
    price_cents = 15000


class JobAssignmentFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = "scheduling.JobAssignment"

    organization = factory.SubFactory(OrganizationFactory)
    job = factory.SubFactory(JobFactory, organization=factory.SelfAttribute("..organization"))
    user = factory.SubFactory(UserFactory)


class TimeEntryFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = "scheduling.TimeEntry"

    organization = factory.SubFactory(OrganizationFactory)
    job = factory.SubFactory(JobFactory, organization=factory.SelfAttribute("..organization"))
    user = factory.SubFactory(UserFactory)
    clock_in = factory.LazyFunction(timezone.now)


class JobNoteFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = "scheduling.JobNote"

    organization = factory.SubFactory(OrganizationFactory)
    job = factory.SubFactory(JobFactory, organization=factory.SelfAttribute("..organization"))
    user = factory.SubFactory(UserFactory)
    body = "Kitchen tap is dripping."
