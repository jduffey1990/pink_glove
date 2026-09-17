"""
What the billing tables refuse on their own, before any service is involved.

The rules with teeth here are the ones that protect a document somebody has
already seen: an issued invoice is not deleted, a payment is never deleted at
all, and a number is spent once.
"""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from billing.enums import InvoiceStatus, LineKind, PaymentMethod
from billing.models import Invoice, InvoiceLine, InvoiceSequence, Payment
from billing.tests.factories import (
    InvoiceFactory,
    InvoiceLineFactory,
    IssuedInvoiceFactory,
    PaymentFactory,
)
from scheduling.tests.factories import CustomerFactory, JobFactory, OrganizationFactory


@pytest.mark.django_db
class TestInvoiceDelete:
    def test_a_draft_can_be_thrown_away(self):
        invoice = InvoiceFactory()

        invoice.delete()

        assert Invoice.objects.filter(pk=invoice.pk).count() == 0
        assert Invoice.all_objects.get(pk=invoice.pk).deleted_at is not None

    def test_an_issued_invoice_cannot_be_deleted(self):
        invoice = IssuedInvoiceFactory()

        with pytest.raises(ValidationError) as excinfo:
            invoice.delete()

        assert "void" in str(excinfo.value).lower()
        assert Invoice.objects.filter(pk=invoice.pk).exists()

    def test_nor_can_a_void_one(self):
        invoice = IssuedInvoiceFactory(status=InvoiceStatus.VOID)

        with pytest.raises(ValidationError):
            invoice.delete()


@pytest.mark.django_db
class TestInvoiceNumber:
    def test_is_empty_on_a_draft(self):
        assert InvoiceFactory().number == ""

    def test_is_unique_within_an_organization(self):
        organization = OrganizationFactory()
        IssuedInvoiceFactory(organization=organization, number="INV-0001")

        with pytest.raises(IntegrityError):
            IssuedInvoiceFactory(organization=organization, number="INV-0001")

    def test_two_organizations_may_both_have_their_own_0001(self):
        IssuedInvoiceFactory(number="INV-0001")
        IssuedInvoiceFactory(number="INV-0001")

        assert Invoice.objects.filter(number="INV-0001").count() == 2

    def test_several_drafts_coexist_despite_all_having_no_number(self):
        """The unique constraint is partial on `number != ""`."""
        organization = OrganizationFactory()
        InvoiceFactory.create_batch(3, organization=organization)

        assert Invoice.objects.filter(organization=organization).count() == 3


@pytest.mark.django_db
class TestInvoiceLine:
    def test_a_visit_line_may_not_be_negative(self):
        line = InvoiceLineFactory.build(kind=LineKind.VISIT, amount_cents=-500)

        with pytest.raises(ValidationError) as excinfo:
            line.clean()

        assert "amount_cents" in excinfo.value.message_dict

    def test_an_adjustment_may_be_negative(self):
        invoice = InvoiceFactory()
        line = InvoiceLine(
            organization=invoice.organization,
            invoice=invoice,
            kind=LineKind.ADJUSTMENT,
            description="Goodwill discount",
            amount_cents=-2500,
        )

        line.clean()
        line.save()

        assert InvoiceLine.objects.get(pk=line.pk).amount_cents == -2500

    def test_the_database_refuses_a_negative_visit_even_without_clean(self):
        invoice = InvoiceFactory()
        job = JobFactory(organization=invoice.organization, customer=invoice.customer)

        with pytest.raises(IntegrityError), transaction.atomic():
            InvoiceLine.objects.create(
                organization=invoice.organization,
                invoice=invoice,
                job=job,
                kind=LineKind.VISIT,
                description="Standard clean",
                amount_cents=-1,
            )

    def test_only_an_adjustment_may_stand_without_a_job(self):
        invoice = InvoiceFactory()

        with pytest.raises(IntegrityError), transaction.atomic():
            InvoiceLine.objects.create(
                organization=invoice.organization,
                invoice=invoice,
                kind=LineKind.VISIT,
                description="Standard clean",
                amount_cents=15000,
            )

    def test_a_line_cannot_bill_another_customers_visit(self):
        invoice = InvoiceFactory()
        other_customer = CustomerFactory(organization=invoice.organization)
        job = JobFactory(organization=invoice.organization, customer=other_customer)

        line = InvoiceLine(
            organization=invoice.organization,
            invoice=invoice,
            job=job,
            kind=LineKind.VISIT,
            description="Standard clean",
            amount_cents=15000,
        )

        with pytest.raises(ValidationError) as excinfo:
            line.clean()

        assert "job" in excinfo.value.message_dict

    def test_a_line_cannot_bill_another_organizations_visit(self):
        """`TenantModel` covers this one -- the FK points outside the tenant."""
        invoice = InvoiceFactory()
        rival_job = JobFactory()

        with pytest.raises(ValidationError):
            InvoiceLine.objects.create(
                organization=invoice.organization,
                invoice=invoice,
                job=rival_job,
                kind=LineKind.VISIT,
                description="Standard clean",
                amount_cents=15000,
            )

    def test_lines_come_back_in_position_order(self):
        invoice = InvoiceFactory()
        for position in (2, 0, 1):
            InvoiceLineFactory(
                organization=invoice.organization,
                invoice=invoice,
                position=position,
                description=f"Line {position}",
            )

        assert [line.position for line in invoice.lines.all()] == [0, 1, 2]

    def test_soft_deleting_an_invoice_leaves_its_lines_alone(self):
        """
        The FK cascades, but `delete()` is a soft delete and so cascades
        nothing. Worth pinning: a caller that discards a draft and then counts
        `InvoiceLine` rows will find them, and the one-line-per-job rule in
        `services` has to filter on the invoice rather than trust the count.
        """
        invoice = InvoiceFactory()
        InvoiceLineFactory(organization=invoice.organization, invoice=invoice)

        invoice.delete()

        assert InvoiceLine.objects.filter(invoice=invoice).exists()


@pytest.mark.django_db
class TestPayment:
    def test_is_never_deleted_only_voided(self):
        payment = PaymentFactory()

        with pytest.raises(ValidationError) as excinfo:
            payment.delete()

        assert "voided" in str(excinfo.value).lower()
        assert Payment.objects.filter(pk=payment.pk).exists()

    def test_not_even_hard_deleted(self):
        payment = PaymentFactory()

        with pytest.raises(ValidationError):
            payment.hard_delete()

    def test_must_be_for_more_than_nothing(self):
        invoice = IssuedInvoiceFactory()

        with pytest.raises(IntegrityError), transaction.atomic():
            Payment.objects.create(
                organization=invoice.organization,
                invoice=invoice,
                method=PaymentMethod.CASH,
                amount_cents=0,
                received_on=invoice.issued_on,
            )

    def test_a_tip_may_stand_beside_any_amount(self):
        payment = PaymentFactory(amount_cents=15000, tip_cents=2000)

        assert payment.tip_cents == 2000

    def test_a_providers_reference_is_claimed_once(self):
        """The webhook idempotency key: one event, one payment row."""
        first = PaymentFactory(provider="stripe", provider_reference="pi_abc123")

        with pytest.raises(IntegrityError), transaction.atomic():
            PaymentFactory(
                organization=first.organization,
                invoice=first.invoice,
                provider="stripe",
                provider_reference="pi_abc123",
            )

    def test_a_reference_without_a_provider_is_not_claimed(self):
        """Two customers may both write check number 401."""
        first = PaymentFactory(reference="401")
        second = PaymentFactory(reference="401")

        assert first.pk != second.pk

    def test_many_manual_payments_coexist_with_no_provider(self):
        invoice = IssuedInvoiceFactory()
        PaymentFactory.create_batch(
            3, organization=invoice.organization, invoice=invoice, amount_cents=1000
        )

        assert invoice.payments.count() == 3


@pytest.mark.django_db
class TestInvoiceSequence:
    def test_there_is_one_per_organization(self):
        organization = OrganizationFactory()
        InvoiceSequence.objects.create(organization=organization)

        with pytest.raises(IntegrityError):
            InvoiceSequence.objects.create(organization=organization)

    def test_starts_at_one(self):
        sequence = InvoiceSequence.objects.create(organization=OrganizationFactory())

        assert sequence.next_number == 1


@pytest.mark.django_db
class TestTenantConsistency:
    def test_an_invoice_cannot_bill_another_organizations_customer(self):
        organization = OrganizationFactory()
        rival_customer = CustomerFactory()

        with pytest.raises(ValidationError):
            Invoice.objects.create(organization=organization, customer=rival_customer)

    def test_a_payment_cannot_point_at_another_organizations_invoice(self):
        organization = OrganizationFactory()
        rival_invoice = IssuedInvoiceFactory()

        with pytest.raises(ValidationError):
            Payment.objects.create(
                organization=organization,
                invoice=rival_invoice,
                method=PaymentMethod.CASH,
                amount_cents=100,
                received_on=rival_invoice.issued_on,
            )


@pytest.mark.django_db
def test_the_snapshot_fields_default_to_an_empty_draft():
    invoice = InvoiceFactory()

    assert invoice.status == InvoiceStatus.DRAFT
    assert invoice.issued_on is None and invoice.due_on is None
    assert invoice.tax_rate_percent == Decimal("0.000")
    assert (invoice.subtotal_cents, invoice.tax_cents, invoice.total_cents) == (0, 0, 0)
    assert invoice.sent_at is None
