"""
The billing rules.

The money cases are the point of this file: tax rounded once over the whole
taxable subtotal rather than per line, a percentage no-access fee landing on
half a cent, a discount large enough to take the taxable subtotal negative, and
two dispatchers issuing at the same moment.
"""

import datetime as dt
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from app.exceptions import ConflictError
from billing import services
from billing.enums import InvoiceStatus, LineKind, PaymentMethod, PaymentState
from billing.models import Invoice, InvoiceLine, InvoiceSequence, Payment
from billing.tests.conftest import completed_job
from billing.tests.factories import InvoiceFactory, InvoiceLineFactory
from catalog.enums import PricingModel
from organizations.enums import NoAccessFeeType
from scheduling.enums import JobStatus
from scheduling.tests.factories import (
    CustomerFactory,
    JobFactory,
    OrganizationFactory,
    ServiceFactory,
    TimeEntryFactory,
    UserFactory,
)

# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestBillableJobs:
    def test_lists_completed_visits_oldest_first(self, org, customer, service):
        second = completed_job(org, customer, service, days_ago=1)
        first = completed_job(org, customer, service, days_ago=5)

        assert list(services.billable_jobs(org)) == [first, second]

    def test_ignores_visits_that_are_not_finished(self, org, customer, service):
        for status in (JobStatus.SCHEDULED, JobStatus.EN_ROUTE, JobStatus.IN_PROGRESS):
            JobFactory(organization=org, customer=customer, service=service, status=status)

        assert list(services.billable_jobs(org)) == []

    def test_ignores_a_cancelled_visit(self, org, customer, service):
        JobFactory(organization=org, customer=customer, service=service, status=JobStatus.CANCELLED)

        assert list(services.billable_jobs(org)) == []

    def test_a_no_access_visit_appears_only_where_it_is_charged_for(self, org, customer, service):
        JobFactory(organization=org, customer=customer, service=service, status=JobStatus.NO_ACCESS)

        assert list(services.billable_jobs(org)) == []

        org.no_access_fee_type = NoAccessFeeType.FLAT
        org.no_access_fee_value = Decimal("2500")
        org.save()

        assert len(services.billable_jobs(org)) == 1

    def test_a_fee_switched_on_but_set_to_zero_still_hides_it(self, org, customer, service):
        """Otherwise the visit sits in the list forever, unbillable."""
        JobFactory(organization=org, customer=customer, service=service, status=JobStatus.NO_ACCESS)
        org.no_access_fee_type = NoAccessFeeType.FLAT
        org.no_access_fee_value = Decimal("0")
        org.save()

        assert list(services.billable_jobs(org)) == []

    def test_a_visit_on_a_live_invoice_drops_out(self, org, customer, service):
        job = completed_job(org, customer, service)
        services.draft_invoice(customer=customer, jobs=[job])

        assert list(services.billable_jobs(org)) == []

    def test_a_visit_comes_back_when_its_invoice_is_voided(self, org, customer, service):
        job = completed_job(org, customer, service)
        invoice = services.draft_invoice(customer=customer, jobs=[job])
        services.issue_invoice(invoice)

        services.void_invoice(invoice, reason="Billed to the wrong address")

        assert list(services.billable_jobs(org)) == [job]

    def test_a_visit_comes_back_when_its_draft_is_deleted(self, org, customer, service):
        job = completed_job(org, customer, service)
        invoice = services.draft_invoice(customer=customer, jobs=[job])

        invoice.delete()

        assert list(services.billable_jobs(org)) == [job]

    def test_an_adjustment_line_does_not_empty_the_list(self, org, customer, service):
        """
        `job_id` is null on an adjustment, and a NULL in a NOT IN subquery
        makes the whole comparison NULL. Without an explicit isnull guard, one
        discount anywhere would hide every billable visit in the organization.
        """
        billable = completed_job(org, customer, service)
        invoice = InvoiceFactory(organization=org, customer=customer)
        InvoiceLine.objects.create(
            organization=org,
            invoice=invoice,
            kind=LineKind.ADJUSTMENT,
            description="Goodwill discount",
            amount_cents=-500,
        )

        assert list(services.billable_jobs(org)) == [billable]

    def test_is_scoped_to_one_organization(self, org, customer, service):
        mine = completed_job(org, customer, service)
        rival_org = OrganizationFactory()
        rival_customer = CustomerFactory(organization=rival_org)
        JobFactory(organization=rival_org, customer=rival_customer, status=JobStatus.COMPLETE)

        assert list(services.billable_jobs(org)) == [mine]

    def test_can_be_narrowed_to_one_customer(self, org, customer, service):
        mine = completed_job(org, customer, service)
        other = CustomerFactory(organization=org)
        completed_job(org, other, service)

        assert list(services.billable_jobs(org, customer=customer)) == [mine]


# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDraftInvoice:
    def test_makes_one_line_per_visit_at_the_price_agreed_then(self, org, customer, service):
        job = completed_job(org, customer, service, price_cents=15000)
        service.base_price_cents = 99000  # a price rise after the visit
        service.save()

        invoice = services.draft_invoice(customer=customer, jobs=[job])

        (line,) = invoice.lines.all()
        assert line.kind == LineKind.VISIT
        assert line.amount_cents == 15000
        assert line.job_id == job.pk

    def test_the_description_names_the_service_and_the_local_day(self, org, customer, service):
        # 01:30 UTC on the 15th is the evening of the 14th in Denver, and the
        # invoice has to say the 14th.
        start = dt.datetime(2027, 6, 15, 1, 30, tzinfo=dt.UTC)
        job = JobFactory(
            organization=org,
            customer=customer,
            service=service,
            status=JobStatus.COMPLETE,
            scheduled_start=start,
            scheduled_end=start + dt.timedelta(hours=2),
        )

        invoice = services.draft_invoice(customer=customer, jobs=[job])

        assert invoice.lines.get().description == "Standard clean -- Mon 14 Jun 2027"

    def test_snapshots_whether_the_service_was_taxable(self, org, customer, service):
        service.is_taxable = True
        service.save()
        job = completed_job(org, customer, service)

        invoice = services.draft_invoice(customer=customer, jobs=[job])
        line = invoice.lines.get()

        service.is_taxable = False
        service.save()
        line.refresh_from_db()

        assert line.is_taxable is True

    def test_a_no_access_visit_bills_the_fee_not_the_visit(self, org, customer, service):
        org.no_access_fee_type = NoAccessFeeType.FLAT
        org.no_access_fee_value = Decimal("2500")
        org.save()
        job = JobFactory(
            organization=org,
            customer=customer,
            service=service,
            status=JobStatus.NO_ACCESS,
            price_cents=15000,
        )

        invoice = services.draft_invoice(customer=customer, jobs=[job])

        (line,) = invoice.lines.all()
        assert line.kind == LineKind.NO_ACCESS_FEE
        assert line.amount_cents == 2500
        assert line.description.startswith("No access -- ")

    def test_a_percentage_fee_is_taken_off_that_visits_price(self, org, customer, service):
        org.no_access_fee_type = NoAccessFeeType.PERCENT
        org.no_access_fee_value = Decimal("33.333")
        org.save()
        # 33.333% of 150 cents is 49.9995; rounded once, half up, that is 50.
        job = JobFactory(
            organization=org,
            customer=customer,
            service=service,
            status=JobStatus.NO_ACCESS,
            price_cents=150,
        )

        invoice = services.draft_invoice(customer=customer, jobs=[job])

        assert invoice.lines.get().amount_cents == 50

    def test_refuses_a_visit_belonging_to_another_customer(self, org, customer, service):
        other = CustomerFactory(organization=org)
        job = completed_job(org, other, service)

        with pytest.raises(ValidationError) as excinfo:
            services.draft_invoice(customer=customer, jobs=[job])

        assert "another customer" in str(excinfo.value)

    def test_refuses_a_visit_from_another_organization(self, org, customer, service):
        rival_org = OrganizationFactory()
        rival_job = JobFactory(
            organization=rival_org,
            customer=CustomerFactory(organization=rival_org),
            status=JobStatus.COMPLETE,
        )

        with pytest.raises(ValidationError):
            services.draft_invoice(customer=customer, jobs=[rival_job])

    def test_refuses_a_visit_that_is_not_finished(self, org, customer, service):
        job = JobFactory(
            organization=org, customer=customer, service=service, status=JobStatus.SCHEDULED
        )

        with pytest.raises(ValidationError):
            services.draft_invoice(customer=customer, jobs=[job])

    def test_refuses_a_visit_already_on_a_live_invoice(self, org, customer, service):
        job = completed_job(org, customer, service)
        services.draft_invoice(customer=customer, jobs=[job])

        with pytest.raises(ConflictError) as excinfo:
            services.draft_invoice(customer=customer, jobs=[job])

        assert excinfo.value.payload["jobs"] == [str(job.pk)]

    def test_allows_a_visit_whose_earlier_invoice_was_voided(self, org, customer, service):
        job = completed_job(org, customer, service)
        first = services.draft_invoice(customer=customer, jobs=[job])
        services.issue_invoice(first)
        services.void_invoice(first, reason="Wrong customer")

        second = services.draft_invoice(customer=customer, jobs=[job])

        assert second.lines.get().job_id == job.pk

    def test_refuses_an_empty_selection(self, org, customer):
        with pytest.raises(ValidationError):
            services.draft_invoice(customer=customer, jobs=[])

    def test_refuses_a_selection_that_produces_no_chargeable_line(self, org, customer, service):
        """A no-access visit at an organization that does not charge for one."""
        job = JobFactory(
            organization=org, customer=customer, service=service, status=JobStatus.NO_ACCESS
        )

        with pytest.raises(ValidationError) as excinfo:
            services.draft_invoice(customer=customer, jobs=[job])

        assert "nothing to invoice" in str(excinfo.value)
        assert Invoice.objects.count() == 0

    def test_lines_are_ordered_by_when_the_visit_happened(self, org, customer, service):
        later = completed_job(org, customer, service, days_ago=1)
        earlier = completed_job(org, customer, service, days_ago=9)

        invoice = services.draft_invoice(customer=customer, jobs=[later, earlier])

        assert [line.job_id for line in invoice.lines.all()] == [earlier.pk, later.pk]


# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTotals:
    def test_an_untaxed_invoice_is_just_its_lines(self, org, customer):
        invoice = InvoiceFactory(organization=org, customer=customer)
        for amount in (15000, 8000):
            InvoiceLineFactory(
                organization=org, invoice=invoice, amount_cents=amount, is_taxable=False
            )

        assert services.totals(invoice) == {
            "subtotal_cents": 23000,
            "tax_cents": 0,
            "total_cents": 23000,
        }

    def test_tax_is_rounded_once_over_the_whole_taxable_subtotal(self, org, customer):
        """
        Three lines at 333 cents and 8.25%: each line alone is 27.4725 cents,
        which rounds to 27 and sums to 81. Over the subtotal of 999 it is
        82.4175, which rounds to 82. The invoice-level answer is the right one,
        and per-line rounding would quietly undercharge on every invoice.
        """
        org.tax_rate_percent = Decimal("8.250")
        org.save()
        invoice = InvoiceFactory(organization=org, customer=customer)
        for _ in range(3):
            InvoiceLineFactory(organization=org, invoice=invoice, amount_cents=333, is_taxable=True)

        assert services.totals(invoice) == {
            "subtotal_cents": 999,
            "tax_cents": 82,
            "total_cents": 1081,
        }

    def test_only_taxable_lines_are_taxed(self, org, customer):
        org.tax_rate_percent = Decimal("10.000")
        org.save()
        invoice = InvoiceFactory(organization=org, customer=customer)
        InvoiceLineFactory(organization=org, invoice=invoice, amount_cents=10000, is_taxable=False)
        InvoiceLineFactory(organization=org, invoice=invoice, amount_cents=5000, is_taxable=True)

        assert services.totals(invoice) == {
            "subtotal_cents": 15000,
            "tax_cents": 500,
            "total_cents": 15500,
        }

    def test_a_discount_bigger_than_the_taxable_lines_floors_the_tax_at_zero(self, org, customer):
        """Not a negative tax -- no authority issues a credit that way."""
        org.tax_rate_percent = Decimal("10.000")
        org.save()
        invoice = InvoiceFactory(organization=org, customer=customer)
        InvoiceLineFactory(organization=org, invoice=invoice, amount_cents=5000, is_taxable=True)
        InvoiceLine.objects.create(
            organization=org,
            invoice=invoice,
            kind=LineKind.ADJUSTMENT,
            description="Goodwill",
            amount_cents=-6000,
            is_taxable=True,
        )

        assert services.totals(invoice) == {
            "subtotal_cents": -1000,
            "tax_cents": 0,
            "total_cents": -1000,
        }

    def test_a_half_cent_of_tax_rounds_up(self, org, customer):
        org.tax_rate_percent = Decimal("50.000")
        org.save()
        invoice = InvoiceFactory(organization=org, customer=customer)
        InvoiceLineFactory(organization=org, invoice=invoice, amount_cents=1, is_taxable=True)

        assert services.totals(invoice)["tax_cents"] == 1

    def test_a_draft_follows_the_organizations_current_rate(self, org, customer):
        invoice = InvoiceFactory(organization=org, customer=customer)
        InvoiceLineFactory(organization=org, invoice=invoice, amount_cents=10000, is_taxable=True)

        assert services.totals(invoice)["tax_cents"] == 0

        org.tax_rate_percent = Decimal("10.000")
        org.save()
        invoice.refresh_from_db()

        assert services.totals(invoice)["tax_cents"] == 1000

    def test_an_issued_invoice_keeps_its_own_rate_forever(self, org, customer, service):
        org.tax_rate_percent = Decimal("8.250")
        org.save()
        job = completed_job(org, customer, service, price_cents=10000)
        service.is_taxable = True
        service.save()
        job.refresh_from_db()

        invoice = services.draft_invoice(customer=customer, jobs=[job])
        invoice.lines.update(is_taxable=True)
        services.issue_invoice(invoice)
        issued_tax = invoice.tax_cents

        org.tax_rate_percent = Decimal("20.000")
        org.save()
        invoice.refresh_from_db()

        assert issued_tax == 825
        assert services.totals(invoice)["tax_cents"] == 825


# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestIssueInvoice:
    def test_numbers_the_invoice_from_the_organizations_prefix(self, org, customer, service):
        org.invoice_prefix = "SPK"
        org.save()
        job = completed_job(org, customer, service)
        invoice = services.draft_invoice(customer=customer, jobs=[job])

        services.issue_invoice(invoice)

        assert invoice.number == "SPK-0001"
        assert invoice.status == InvoiceStatus.ISSUED

    def test_numbers_run_consecutively(self, org, customer, service):
        for _ in range(3):
            job = completed_job(org, customer, service)
            services.issue_invoice(services.draft_invoice(customer=customer, jobs=[job]))

        assert list(Invoice.objects.order_by("number").values_list("number", flat=True)) == [
            "INV-0001",
            "INV-0002",
            "INV-0003",
        ]

    def test_a_voided_invoice_keeps_its_number_spent(self, org, customer, service):
        first = services.issue_invoice(
            services.draft_invoice(customer=customer, jobs=[completed_job(org, customer, service)])
        )
        services.void_invoice(first, reason="Mistake")

        second = services.issue_invoice(
            services.draft_invoice(customer=customer, jobs=[completed_job(org, customer, service)])
        )

        assert (first.number, second.number) == ("INV-0001", "INV-0002")

    def test_each_organization_numbers_from_one(self, org, customer, service):
        services.issue_invoice(
            services.draft_invoice(customer=customer, jobs=[completed_job(org, customer, service)])
        )

        other_org = OrganizationFactory()
        other_customer = CustomerFactory(organization=other_org)
        other_service = ServiceFactory(organization=other_org)
        theirs = services.issue_invoice(
            services.draft_invoice(
                customer=other_customer,
                jobs=[completed_job(other_org, other_customer, other_service)],
            )
        )

        assert theirs.number == "INV-0001"
        assert InvoiceSequence.objects.count() == 2

    def test_dates_are_the_organizations_own(self, org, customer, service):
        org.invoice_terms_days = 30
        org.save()
        job = completed_job(org, customer, service)
        invoice = services.draft_invoice(customer=customer, jobs=[job])

        services.issue_invoice(invoice, today=dt.date(2027, 6, 14))

        assert invoice.issued_on == dt.date(2027, 6, 14)
        assert invoice.due_on == dt.date(2027, 7, 14)

    def test_freezes_the_bill_to_details(self, org, customer, service):
        job = completed_job(org, customer, service)
        invoice = services.draft_invoice(customer=customer, jobs=[job])

        services.issue_invoice(invoice)
        customer.last_name = "Moved-Away"
        customer.billing_line1 = "99 Elsewhere Road"
        customer.save()
        invoice.refresh_from_db()

        assert invoice.bill_to_name == "Dana Henderson"
        assert "14 Oak Street" in invoice.bill_to_address
        assert invoice.bill_to_email == "dana@example.com"

    def test_freezes_the_amounts(self, org, customer, service):
        job = completed_job(org, customer, service, price_cents=15000)
        invoice = services.draft_invoice(customer=customer, jobs=[job])

        services.issue_invoice(invoice)

        assert (invoice.subtotal_cents, invoice.tax_cents, invoice.total_cents) == (
            15000,
            0,
            15000,
        )

    def test_records_who_opened_it_and_who_issued_it(self, org, customer, service):
        """
        Not always the same person: a dispatcher prepares the month and an
        owner signs it off.
        """
        opener, issuer = UserFactory(), UserFactory()
        job = completed_job(org, customer, service)
        invoice = services.draft_invoice(customer=customer, jobs=[job], actor=opener)

        services.issue_invoice(invoice, actor=issuer)

        assert invoice.created_by == opener
        assert invoice.issued_by == issuer

    def test_a_draft_has_nobody_down_as_having_issued_it(self, org, customer, service):
        job = completed_job(org, customer, service)

        invoice = services.draft_invoice(customer=customer, jobs=[job], actor=UserFactory())

        assert invoice.issued_by is None

    def test_an_invoice_with_no_lines_is_refused(self, org, customer):
        invoice = InvoiceFactory(organization=org, customer=customer)

        with pytest.raises(ConflictError):
            services.issue_invoice(invoice)

    def test_an_invoice_coming_to_nothing_is_refused(self, org, customer):
        invoice = InvoiceFactory(organization=org, customer=customer)
        InvoiceLine.objects.create(
            organization=org,
            invoice=invoice,
            kind=LineKind.ADJUSTMENT,
            description="Nothing at all",
            amount_cents=0,
        )

        with pytest.raises(ConflictError):
            services.issue_invoice(invoice)

    def test_issuing_twice_is_refused(self, org, customer, service):
        job = completed_job(org, customer, service)
        invoice = services.issue_invoice(services.draft_invoice(customer=customer, jobs=[job]))

        with pytest.raises(ConflictError) as excinfo:
            services.issue_invoice(invoice)

        assert excinfo.value.payload["status"] == InvoiceStatus.ISSUED

    def test_two_concurrent_issues_take_different_numbers(self, org, customer, service):
        """
        Serialized here rather than genuinely parallel -- the point is that the
        sequence is the source of the number, not `max(number) + 1` over a
        table that a second transaction is writing to.
        """
        first = services.draft_invoice(
            customer=customer, jobs=[completed_job(org, customer, service)]
        )
        second = services.draft_invoice(
            customer=customer, jobs=[completed_job(org, customer, service)]
        )

        services.issue_invoice(first)
        services.issue_invoice(second)

        assert first.number != second.number
        assert InvoiceSequence.objects.get(organization=org).next_number == 3


# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestVoidInvoice:
    @pytest.fixture
    def issued(self, org, customer, service):
        return services.issue_invoice(
            services.draft_invoice(customer=customer, jobs=[completed_job(org, customer, service)])
        )

    def test_records_who_and_why(self, issued, org):
        actor = UserFactory()

        services.void_invoice(issued, actor=actor, reason="Billed the wrong address")

        assert issued.status == InvoiceStatus.VOID
        assert issued.voided_by == actor
        assert issued.void_reason == "Billed the wrong address"
        assert issued.voided_at is not None

    def test_needs_a_reason(self, issued):
        with pytest.raises(ValidationError) as excinfo:
            services.void_invoice(issued, reason="   ")

        assert "reason" in excinfo.value.message_dict

    def test_a_draft_is_deleted_not_voided(self, org, customer):
        draft = InvoiceFactory(organization=org, customer=customer)

        with pytest.raises(ConflictError) as excinfo:
            services.void_invoice(draft, reason="Mistake")

        assert "Delete it instead" in excinfo.value.detail

    def test_voiding_twice_is_refused(self, issued):
        services.void_invoice(issued, reason="Mistake")

        with pytest.raises(ConflictError):
            services.void_invoice(issued, reason="Again")

    def test_is_refused_while_money_points_at_it(self, issued, org):
        services.record_payment(
            issued,
            method=PaymentMethod.CHECK,
            amount_cents=issued.total_cents,
            received_on=org.today(),
        )

        with pytest.raises(ConflictError) as excinfo:
            services.void_invoice(issued, reason="Mistake")

        assert "Void those first" in excinfo.value.detail

    def test_is_allowed_once_the_payments_are_voided(self, issued, org):
        payment = services.record_payment(
            issued,
            method=PaymentMethod.CHECK,
            amount_cents=issued.total_cents,
            received_on=org.today(),
        )
        services.void_payment(payment, reason="Check bounced")

        services.void_invoice(issued, reason="Starting again")

        assert issued.status == InvoiceStatus.VOID


# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestPayments:
    @pytest.fixture
    def issued(self, org, customer, service):
        return services.issue_invoice(
            services.draft_invoice(
                customer=customer,
                jobs=[completed_job(org, customer, service, price_cents=15000)],
            )
        )

    def test_a_draft_cannot_take_a_payment(self, org, customer):
        draft = InvoiceFactory(organization=org, customer=customer)

        with pytest.raises(ConflictError):
            services.record_payment(
                draft,
                method=PaymentMethod.CASH,
                amount_cents=100,
                received_on=org.today(),
            )

    def test_a_void_invoice_cannot_take_a_payment(self, issued, org):
        services.void_invoice(issued, reason="Mistake")

        with pytest.raises(ConflictError):
            services.record_payment(
                issued,
                method=PaymentMethod.CASH,
                amount_cents=100,
                received_on=org.today(),
            )

    def test_paying_in_full_settles_it(self, issued, org):
        services.record_payment(
            issued, method=PaymentMethod.CHECK, amount_cents=15000, received_on=org.today()
        )

        assert services.payment_state(issued) == PaymentState.PAID
        assert services.balance_cents(issued) == 0

    def test_a_partial_payment_leaves_a_balance(self, issued, org):
        services.record_payment(
            issued, method=PaymentMethod.CASH, amount_cents=5000, received_on=org.today()
        )

        assert services.payment_state(issued) == PaymentState.PARTIAL
        assert services.balance_cents(issued) == 10000

    def test_payments_add_up(self, issued, org):
        for amount in (5000, 5000, 5000):
            services.record_payment(
                issued, method=PaymentMethod.CASH, amount_cents=amount, received_on=org.today()
            )

        assert services.payment_state(issued) == PaymentState.PAID

    def test_more_than_the_balance_is_refused_and_names_the_field(self, issued, org):
        with pytest.raises(ValidationError) as excinfo:
            services.record_payment(
                issued,
                method=PaymentMethod.CASH,
                amount_cents=15001,
                received_on=org.today(),
            )

        assert "amount_cents" in excinfo.value.message_dict
        assert "tip_cents" in str(excinfo.value)

    def test_a_tip_rides_alongside_without_settling_anything(self, issued, org):
        services.record_payment(
            issued,
            method=PaymentMethod.CASH,
            amount_cents=5000,
            tip_cents=2000,
            received_on=org.today(),
        )

        assert services.paid_cents(issued) == 5000
        assert services.balance_cents(issued) == 10000

    def test_voiding_a_payment_brings_the_balance_back(self, issued, org):
        payment = services.record_payment(
            issued, method=PaymentMethod.CHECK, amount_cents=15000, received_on=org.today()
        )
        assert services.payment_state(issued) == PaymentState.PAID

        services.void_payment(payment, reason="Check bounced")

        assert services.payment_state(issued) == PaymentState.UNPAID
        assert services.balance_cents(issued) == 15000
        assert Payment.objects.filter(pk=payment.pk).exists()

    def test_and_the_invoice_can_then_be_paid_again(self, issued, org):
        first = services.record_payment(
            issued, method=PaymentMethod.CHECK, amount_cents=15000, received_on=org.today()
        )
        services.void_payment(first, reason="Check bounced")

        services.record_payment(
            issued, method=PaymentMethod.CASH, amount_cents=15000, received_on=org.today()
        )

        assert services.payment_state(issued) == PaymentState.PAID

    def test_voiding_needs_a_reason(self, issued, org):
        payment = services.record_payment(
            issued, method=PaymentMethod.CASH, amount_cents=100, received_on=org.today()
        )

        with pytest.raises(ValidationError) as excinfo:
            services.void_payment(payment, reason="")

        assert "reason" in excinfo.value.message_dict

    def test_voiding_twice_is_refused(self, issued, org):
        payment = services.record_payment(
            issued, method=PaymentMethod.CASH, amount_cents=100, received_on=org.today()
        )
        services.void_payment(payment, reason="Mistake")

        with pytest.raises(ConflictError):
            services.void_payment(payment, reason="Again")

    def test_records_who_took_the_money(self, issued, org):
        actor = UserFactory()

        payment = services.record_payment(
            issued,
            method=PaymentMethod.CASH,
            amount_cents=100,
            received_on=org.today(),
            actor=actor,
            reference="Check 401",
        )

        assert payment.recorded_by == actor
        assert payment.reference == "Check 401"


# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestOverdue:
    @pytest.fixture
    def issued(self, org, customer, service):
        invoice = services.draft_invoice(
            customer=customer, jobs=[completed_job(org, customer, service)]
        )
        return services.issue_invoice(invoice, today=dt.date(2027, 6, 1))

    def test_is_false_before_the_due_date(self, issued):
        assert services.is_overdue(issued, today=dt.date(2027, 6, 15)) is False

    def test_is_false_on_the_due_date_itself(self, issued):
        assert issued.due_on == dt.date(2027, 6, 15)
        assert services.is_overdue(issued, today=issued.due_on) is False

    def test_is_true_the_day_after(self, issued):
        assert services.is_overdue(issued, today=dt.date(2027, 6, 16)) is True

    def test_a_paid_invoice_is_never_overdue(self, issued, org):
        services.record_payment(
            issued,
            method=PaymentMethod.CHECK,
            amount_cents=issued.total_cents,
            received_on=dt.date(2027, 6, 2),
        )

        assert services.is_overdue(issued, today=dt.date(2027, 12, 1)) is False

    def test_a_part_paid_invoice_still_is(self, issued, org):
        services.record_payment(
            issued, method=PaymentMethod.CASH, amount_cents=100, received_on=dt.date(2027, 6, 2)
        )

        assert services.is_overdue(issued, today=dt.date(2027, 6, 16)) is True

    def test_a_draft_is_not_overdue(self, org, customer):
        assert services.is_overdue(InvoiceFactory(organization=org, customer=customer)) is False


# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRepriceFromTimeWorked:
    @pytest.fixture
    def hourly(self, org):
        return ServiceFactory(
            organization=org,
            name="Hourly clean",
            pricing_model=PricingModel.HOURLY,
            hourly_rate_cents=Decimal("5000.00"),
            base_price_cents=6000,
        )

    def _job_with_time(self, org, customer, hourly, *, minutes, crew=1):
        job = completed_job(org, customer, hourly, price_cents=10000)
        for _ in range(crew):
            clock_in = timezone.now() - dt.timedelta(minutes=minutes)
            TimeEntryFactory(
                organization=org,
                job=job,
                user=UserFactory(),
                clock_in=clock_in,
                clock_out=clock_in + dt.timedelta(minutes=minutes),
            )
        return job

    def test_re_prices_from_the_crews_recorded_hours(self, org, customer, hourly):
        job = self._job_with_time(org, customer, hourly, minutes=90)
        invoice = services.draft_invoice(customer=customer, jobs=[job])
        line = invoice.lines.get()
        assert line.amount_cents == 10000

        services.reprice_line_from_time_worked(line)

        # 90 minutes at 5000 cents an hour.
        assert line.amount_cents == 7500

    def test_sums_the_whole_crew(self, org, customer, hourly):
        job = self._job_with_time(org, customer, hourly, minutes=60, crew=2)
        invoice = services.draft_invoice(customer=customer, jobs=[job])
        line = invoice.lines.get()

        services.reprice_line_from_time_worked(line)

        assert line.amount_cents == 10000

    def test_ignores_an_open_entry(self, org, customer, hourly):
        """Someone still clocked in has not worked a knowable number of hours."""
        job = self._job_with_time(org, customer, hourly, minutes=90)
        TimeEntryFactory(organization=org, job=job, user=UserFactory(), clock_out=None)
        invoice = services.draft_invoice(customer=customer, jobs=[job])
        line = invoice.lines.get()

        services.reprice_line_from_time_worked(line)

        # The 90 closed minutes, and nothing for the entry still running.
        assert line.amount_cents == 7500

    def test_floors_at_the_services_minimum_charge(self, org, customer, hourly):
        job = self._job_with_time(org, customer, hourly, minutes=5)
        invoice = services.draft_invoice(customer=customer, jobs=[job])
        line = invoice.lines.get()

        services.reprice_line_from_time_worked(line)

        # 5 minutes is 417 cents; the callout minimum is 6000.
        assert line.amount_cents == 6000

    def test_refuses_a_service_that_is_not_hourly(self, org, customer, service):
        job = completed_job(org, customer, service)
        invoice = services.draft_invoice(customer=customer, jobs=[job])

        with pytest.raises(ConflictError) as excinfo:
            services.reprice_line_from_time_worked(invoice.lines.get())

        assert "not priced hourly" in excinfo.value.detail

    def test_refuses_when_nobody_recorded_any_time(self, org, customer, hourly):
        job = completed_job(org, customer, hourly)
        invoice = services.draft_invoice(customer=customer, jobs=[job])

        with pytest.raises(ConflictError):
            services.reprice_line_from_time_worked(invoice.lines.get())

    def test_refuses_on_an_issued_invoice(self, org, customer, hourly):
        job = self._job_with_time(org, customer, hourly, minutes=90)
        invoice = services.draft_invoice(customer=customer, jobs=[job])
        line = invoice.lines.get()
        services.issue_invoice(invoice)
        line.refresh_from_db()

        with pytest.raises(ConflictError):
            services.reprice_line_from_time_worked(line)

    def test_refuses_an_adjustment_line(self, org, customer):
        invoice = InvoiceFactory(organization=org, customer=customer)
        line = InvoiceLine.objects.create(
            organization=org,
            invoice=invoice,
            kind=LineKind.ADJUSTMENT,
            description="Goodwill",
            amount_cents=-500,
        )

        with pytest.raises(ConflictError):
            services.reprice_line_from_time_worked(line)


# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAvailableActions:
    def test_an_empty_draft_can_only_be_edited_or_thrown_away(self, org, customer):
        invoice = InvoiceFactory(organization=org, customer=customer)

        assert services.available_actions(invoice) == ["edit", "delete"]

    def test_a_draft_with_lines_can_be_issued(self, org, customer, service):
        job = completed_job(org, customer, service)
        invoice = services.draft_invoice(customer=customer, jobs=[job])

        assert "issue" in services.available_actions(invoice)

    def test_an_issued_invoice_can_be_sent_paid_or_voided(self, org, customer, service):
        job = completed_job(org, customer, service)
        invoice = services.issue_invoice(services.draft_invoice(customer=customer, jobs=[job]))

        assert set(services.available_actions(invoice)) == {"send", "record_payment", "void"}

    def test_a_paid_invoice_offers_neither_payment_nor_void(self, org, customer, service):
        job = completed_job(org, customer, service)
        invoice = services.issue_invoice(services.draft_invoice(customer=customer, jobs=[job]))
        services.record_payment(
            invoice,
            method=PaymentMethod.CHECK,
            amount_cents=invoice.total_cents,
            received_on=org.today(),
        )

        assert services.available_actions(invoice) == ["send"]

    def test_a_void_invoice_offers_nothing(self, org, customer, service):
        job = completed_job(org, customer, service)
        invoice = services.issue_invoice(services.draft_invoice(customer=customer, jobs=[job]))
        services.void_invoice(invoice, reason="Mistake")

        assert services.available_actions(invoice) == []


@pytest.mark.django_db
class TestTheRowLocks:
    """
    Each transition reads a status and then writes one. Read off an instance
    nobody holds, the two can disagree -- and every case below was a silent
    200 that left a permanent wrong record.
    """

    @pytest.fixture
    def issued(self, org, customer, service):
        return services.issue_invoice(
            services.draft_invoice(customer=customer, jobs=[completed_job(org, customer, service)])
        )

    def test_issuing_a_draft_twice_from_two_stale_copies_takes_one_number(
        self, org, customer, service
    ):
        """
        Two dispatchers pressing Issue. Both once passed the draft check, both
        took a number, and the first one's number ended up on no document --
        a permanent gap in a sequence ADR-026 says has none.
        """
        invoice = services.draft_invoice(
            customer=customer, jobs=[completed_job(org, customer, service)]
        )
        stale = Invoice.objects.get(pk=invoice.pk)

        services.issue_invoice(invoice)

        with pytest.raises(ConflictError):
            services.issue_invoice(stale)

        assert InvoiceSequence.objects.get(organization=org).next_number == 2
        assert Invoice.objects.filter(number="INV-0001").count() == 1

    def test_voiding_from_a_stale_copy_cannot_strand_a_payment(self, issued, org):
        """
        The copy was read before the payment existed. Unlocked, its `exists()`
        check saw nothing and the invoice went void with live money against
        it -- money that then vanished from every screen, because the balance
        short-circuits on a non-issued invoice.
        """
        stale = Invoice.objects.get(pk=issued.pk)
        services.record_payment(
            issued,
            method=PaymentMethod.CHECK,
            amount_cents=issued.total_cents,
            received_on=org.today(),
        )

        with pytest.raises(ConflictError):
            services.void_invoice(stale, reason="Mistake")

        issued.refresh_from_db()
        assert issued.status == InvoiceStatus.ISSUED

    def test_paying_from_a_stale_copy_cannot_pay_a_void_invoice(self, issued, org):
        stale = Invoice.objects.get(pk=issued.pk)
        services.void_invoice(issued, reason="Billed the wrong address")

        with pytest.raises(ConflictError):
            services.record_payment(
                stale,
                method=PaymentMethod.CASH,
                amount_cents=100,
                received_on=org.today(),
            )

        assert Payment.objects.filter(invoice=issued).count() == 0


@pytest.mark.django_db
class TestBillableJobsAndSoftDeletes:
    """
    The soft-delete manager filters the model being queried and nothing it
    joins to. That trap cost the baseline gate three bugs; this is the same
    one, one join further out.
    """

    def test_a_deleted_customers_visits_leave_the_queue(self, org, customer, service):
        completed_job(org, customer, service)
        assert len(services.billable_jobs(org)) == 1

        customer.delete()

        # Otherwise they sit there for ever, showing a name that was meant to
        # be gone -- and unbillable anyway, because drafting looks the
        # customer up through a manager that does filter.
        assert list(services.billable_jobs(org)) == []

    def test_a_deleted_locations_visits_leave_too(self, org, customer, service):
        job = completed_job(org, customer, service)

        job.location.delete()

        assert list(services.billable_jobs(org)) == []

    def test_a_deleted_services_visits_leave_too(self, org, customer, service):
        completed_job(org, customer, service)

        service.delete()

        assert list(services.billable_jobs(org)) == []
