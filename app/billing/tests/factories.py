"""
factory-boy factories for the billing models.

An invoice needs an organization, a customer under it, lines, the jobs those
lines point at, and payments -- six related rows that must all agree about the
tenant, or `TenantModel.save()` rejects them. `organization` is a trait on
every factory, as in `scheduling.tests.factories`, which these reuse rather
than redefine: a billable job is a scheduling job, and building a second kind
here would let the two drift.
"""

import factory
from django.utils import timezone

from billing.enums import InvoiceStatus, LineKind, PaymentMethod
from scheduling.tests.factories import (
    CustomerFactory,
    JobFactory,
    OrganizationFactory,
    UserFactory,
)


class InvoiceFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = "billing.Invoice"

    organization = factory.SubFactory(OrganizationFactory)
    customer = factory.SubFactory(
        CustomerFactory, organization=factory.SelfAttribute("..organization")
    )
    status = InvoiceStatus.DRAFT


class IssuedInvoiceFactory(InvoiceFactory):
    """
    An invoice that already has its snapshot, for tests about what happens
    *after* issue. Tests about issuing itself call `services.issue_invoice`.
    """

    status = InvoiceStatus.ISSUED
    number = factory.Sequence(lambda n: f"INV-{n + 1:04d}")
    issued_on = factory.LazyFunction(lambda: timezone.now().date())
    due_on = factory.LazyFunction(lambda: timezone.now().date())
    bill_to_name = "Dana Henderson"
    bill_to_email = "dana@example.com"
    subtotal_cents = 15000
    tax_cents = 0
    total_cents = 15000


class InvoiceLineFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = "billing.InvoiceLine"

    organization = factory.SubFactory(OrganizationFactory)
    invoice = factory.SubFactory(
        InvoiceFactory, organization=factory.SelfAttribute("..organization")
    )
    # The job inherits the invoice's customer, so the line's own check passes.
    job = factory.SubFactory(
        JobFactory,
        organization=factory.SelfAttribute("..organization"),
        customer=factory.SelfAttribute("..invoice.customer"),
    )
    kind = LineKind.VISIT
    description = "Standard clean"
    amount_cents = 15000
    is_taxable = False
    position = factory.Sequence(lambda n: n)


class PaymentFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = "billing.Payment"

    organization = factory.SubFactory(OrganizationFactory)
    invoice = factory.SubFactory(
        IssuedInvoiceFactory, organization=factory.SelfAttribute("..organization")
    )
    method = PaymentMethod.CHECK
    amount_cents = 15000
    received_on = factory.LazyFunction(lambda: timezone.now().date())
    recorded_by = factory.SubFactory(UserFactory)


class StripeEventFactory(factory.django.DjangoModelFactory):
    """
    A ledgered webhook event. `organization` is optional on purpose: an
    event for an account nobody owns is ledgered with none (ADR-024).
    """

    class Meta:
        model = "billing.StripeEvent"

    event_id = factory.Sequence(lambda n: f"evt_test_{n:06d}")
    account = "acct_1TestConnectedAcct"
    type = "account.updated"
    organization = None
    payload = factory.LazyAttribute(
        lambda e: {"id": e.event_id, "type": e.type, "account": e.account, "data": {"object": {}}}
    )
