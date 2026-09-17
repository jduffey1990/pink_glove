"""
The invoice email.

What matters here is that the email repeats the snapshot rather than
recomputing it, that `sent_at` is stamped only on a delivery that happened, and
that a task cannot be talked into emailing another tenant's invoice by id.
"""

from decimal import Decimal

import pytest
from django.core import mail
from django.core.exceptions import ValidationError

from app.exceptions import ConflictError
from billing import services
from billing.enums import PaymentMethod
from billing.tasks import send_invoice_email
from billing.tests.conftest import completed_job
from billing.tests.factories import InvoiceFactory
from scheduling.tests.factories import OrganizationFactory


@pytest.fixture
def issued(org, customer, service):
    job = completed_job(org, customer, service, price_cents=15000)
    return services.issue_invoice(services.draft_invoice(customer=customer, jobs=[job]))


@pytest.mark.django_db
class TestSendInvoice:
    def test_sends_to_the_bill_to_address(self, issued, org):
        services.send_invoice(issued)

        (sent,) = mail.outbox
        assert sent.to == ["dana@example.com"]
        assert issued.number in sent.subject
        assert org.name in sent.subject

    def test_the_body_carries_the_lines_totals_and_terms(self, issued, org):
        services.send_invoice(issued)

        body = mail.outbox[0].body
        assert "Standard clean" in body
        assert "$150.00" in body
        assert f"{issued.due_on:%-d %B %Y}" in body
        assert org.invoice_footer in body

    def test_stamps_sent_at_only_once_the_mail_is_away(self, issued):
        assert issued.sent_at is None

        services.send_invoice(issued)
        issued.refresh_from_db()

        assert issued.sent_at is not None

    def test_shows_the_balance_after_a_part_payment(self, issued, org):
        services.record_payment(
            issued, method=PaymentMethod.CASH, amount_cents=5000, received_on=org.today()
        )

        services.send_invoice(issued)

        body = mail.outbox[0].body
        assert "$50.00" in body  # paid
        assert "$100.00" in body  # balance

    def test_says_so_when_it_is_settled(self, issued, org):
        services.record_payment(
            issued,
            method=PaymentMethod.CHECK,
            amount_cents=issued.total_cents,
            received_on=org.today(),
        )

        services.send_invoice(issued)

        assert "paid in full" in mail.outbox[0].body

    def test_repeats_the_snapshot_not_a_fresh_calculation(self, issued, org):
        """A rate change after issue must not alter what the customer reads."""
        org.tax_rate_percent = Decimal("20.000")
        org.save()

        services.send_invoice(issued)

        body = mail.outbox[0].body
        assert "$150.00" in body
        assert "$180.00" not in body

    def test_a_draft_cannot_be_sent(self, org, customer):
        draft = InvoiceFactory(organization=org, customer=customer)

        with pytest.raises(ConflictError):
            services.send_invoice(draft)

        assert mail.outbox == []

    def test_a_void_invoice_cannot_be_sent(self, issued):
        services.void_invoice(issued, reason="Mistake")

        with pytest.raises(ConflictError):
            services.send_invoice(issued)

    def test_an_invoice_with_no_address_names_the_field(self, issued):
        issued.bill_to_email = ""
        issued.save(update_fields=["bill_to_email"])

        with pytest.raises(ValidationError) as excinfo:
            services.send_invoice(issued)

        assert "bill_to_email" in excinfo.value.message_dict
        assert mail.outbox == []


@pytest.mark.django_db
class TestTheTaskItself:
    def test_will_not_send_another_organizations_invoice(self, issued):
        """
        The organization id is what scopes the lookup, not decoration. A stale
        or forged invoice id from another tenant finds nothing.
        """
        rival = OrganizationFactory()

        assert send_invoice_email(str(rival.pk), str(issued.pk)) is False
        assert mail.outbox == []

    def test_will_not_send_an_invoice_that_has_since_been_voided(self, issued):
        services.void_invoice(issued, reason="Mistake")

        assert send_invoice_email(str(issued.organization_id), str(issued.pk)) is False
        assert mail.outbox == []

    def test_an_unknown_invoice_is_a_quiet_false(self, org):
        import uuid

        assert send_invoice_email(str(org.pk), str(uuid.uuid4())) is False

    def test_a_send_that_fails_returns_false_and_stamps_nothing(self, issued, monkeypatch):
        """
        `sent_at` must never claim a delivery that did not happen. A failure
        is logged; the dispatcher presses Send again and nothing about the
        invoice has changed.
        """

        def explode(*args, **kwargs):
            raise OSError("no route to host")

        monkeypatch.setattr("billing.tasks.EmailMessage.send", explode)

        assert send_invoice_email(str(issued.organization_id), str(issued.pk)) is False

        issued.refresh_from_db()
        assert issued.sent_at is None

    def test_the_task_refuses_an_invoice_with_no_address_of_its_own(self, issued):
        """The service guards this too; the task cannot assume it was called."""
        issued.bill_to_email = ""
        issued.save(update_fields=["bill_to_email"])

        assert send_invoice_email(str(issued.organization_id), str(issued.pk)) is False
        assert mail.outbox == []


@pytest.mark.django_db
class TestFormatCents:
    @pytest.mark.parametrize(
        ("cents", "expected"),
        [
            (0, "$0.00"),
            (5, "$0.05"),
            (15000, "$150.00"),
            (123456789, "$1,234,567.89"),
            (-2500, "-$25.00"),
        ],
    )
    def test_reads_as_money(self, cents, expected):
        assert services.format_cents(cents) == expected
