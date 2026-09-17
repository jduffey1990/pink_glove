"""
The billing API.

Every endpoint is dispatcher-and-above, and every one of them is tested from
both sides of that line and across the tenant boundary -- for update, delete
and actions, not only list and retrieve. That was the gap the baseline phase
gate found everywhere else.
"""

import datetime as dt

import pytest
from django.urls import reverse
from django.utils import timezone

from billing import services
from billing.enums import InvoiceStatus, LineKind, PaymentMethod
from billing.models import Invoice, InvoiceLine, Payment
from billing.tests.factories import InvoiceFactory
from scheduling.enums import JobStatus
from scheduling.tests.factories import (
    CustomerFactory,
    JobFactory,
    MembershipFactory,
    OrganizationFactory,
    ServiceFactory,
    UserFactory,
)
from users.enums import Role

INVOICES_URL = reverse("billing:invoice-list")
LINES_URL = reverse("billing:invoiceline-list")
PAYMENTS_URL = reverse("billing:payment-list")
BILLABLE_URL = reverse("billing:invoice-billable-jobs")


def invoice_url(invoice, suffix=""):
    url = reverse("billing:invoice-detail", args=[invoice.pk])
    return f"{url}{suffix}" if suffix else url


def line_url(line, suffix=""):
    url = reverse("billing:invoiceline-detail", args=[line.pk])
    return f"{url}{suffix}" if suffix else url


def payment_url(payment, suffix=""):
    url = reverse("billing:payment-detail", args=[payment.pk])
    return f"{url}{suffix}" if suffix else url


# --- fixtures --------------------------------------------------------------


@pytest.fixture
def org(db):
    return OrganizationFactory(name="Sparkle Clean")


@pytest.fixture
def customer(org):
    return CustomerFactory(
        organization=org, first_name="Dana", last_name="Henderson", email="dana@example.com"
    )


@pytest.fixture
def service(org):
    return ServiceFactory(organization=org, name="Standard clean")


@pytest.fixture
def dispatcher(org):
    user = UserFactory()
    MembershipFactory(user=user, organization=org, role=Role.DISPATCHER)
    return user


@pytest.fixture
def client_as(api_client):
    def _sign_in(user):
        api_client.force_login(user)
        return api_client

    return _sign_in


@pytest.fixture
def dispatcher_client(client_as, dispatcher):
    return client_as(dispatcher)


def make_job(org, customer, service, *, status=JobStatus.COMPLETE, price_cents=15000, days_ago=1):
    start = timezone.now() - dt.timedelta(days=days_ago)
    return JobFactory(
        organization=org,
        customer=customer,
        service=service,
        status=status,
        price_cents=price_cents,
        scheduled_start=start,
        scheduled_end=start + dt.timedelta(hours=2),
    )


@pytest.fixture
def draft(org, customer, service):
    return services.draft_invoice(customer=customer, jobs=[make_job(org, customer, service)])


@pytest.fixture
def issued(org, customer, service):
    invoice = services.draft_invoice(
        customer=customer, jobs=[make_job(org, customer, service, days_ago=4)]
    )
    return services.issue_invoice(invoice)


@pytest.fixture
def rival(db):
    """A whole second tenant: organization, dispatcher, customer and invoice."""
    organization = OrganizationFactory(name="Rival Cleaners")
    user = UserFactory()
    MembershipFactory(user=user, organization=organization, role=Role.DISPATCHER)
    their_customer = CustomerFactory(organization=organization, first_name="Sam")
    their_service = ServiceFactory(organization=organization)
    their_invoice = services.issue_invoice(
        services.draft_invoice(
            customer=their_customer, jobs=[make_job(organization, their_customer, their_service)]
        )
    )
    return {
        "organization": organization,
        "user": user,
        "customer": their_customer,
        "service": their_service,
        "invoice": their_invoice,
    }


# --- the ready-to-invoice list ---------------------------------------------


@pytest.mark.django_db
class TestBillableJobs:
    def test_lists_finished_uninvoiced_visits_with_what_they_will_bill(
        self, dispatcher_client, org, customer, service
    ):
        job = make_job(org, customer, service, price_cents=15000)

        body = dispatcher_client.get(BILLABLE_URL).json()

        assert len(body) == 1
        assert body[0]["id"] == str(job.pk)
        assert body[0]["amount_cents"] == 15000
        assert body[0]["customer_name"] == "Dana Henderson"

    def test_can_be_narrowed_to_one_customer(self, dispatcher_client, org, customer, service):
        make_job(org, customer, service)
        other = CustomerFactory(organization=org, first_name="Alex")
        make_job(org, other, service)

        body = dispatcher_client.get(f"{BILLABLE_URL}?customer={customer.pk}").json()

        assert [row["customer"] for row in body] == [str(customer.pk)]

    def test_shows_nothing_of_another_organizations(self, dispatcher_client, rival):
        body = dispatcher_client.get(BILLABLE_URL).json()

        assert body == []

    def test_another_organizations_customer_id_is_a_404(self, dispatcher_client, rival):
        response = dispatcher_client.get(f"{BILLABLE_URL}?customer={rival['customer'].pk}")

        assert response.status_code == 404


# --- creating a draft ------------------------------------------------------


@pytest.mark.django_db
class TestCreate:
    def test_builds_the_lines_from_the_visits(self, dispatcher_client, org, customer, service):
        job = make_job(org, customer, service, price_cents=15000)

        response = dispatcher_client.post(
            INVOICES_URL, {"customer": str(customer.pk), "jobs": [str(job.pk)]}, format="json"
        )

        assert response.status_code == 201
        assert response.data["status"] == InvoiceStatus.DRAFT
        assert len(response.data["lines"]) == 1
        assert response.data["total_cents"] == 15000

    def test_the_organization_comes_from_the_tenant_not_the_payload(
        self, dispatcher_client, org, customer, service, rival
    ):
        job = make_job(org, customer, service)

        response = dispatcher_client.post(
            INVOICES_URL,
            {
                "customer": str(customer.pk),
                "jobs": [str(job.pk)],
                "organization": str(rival["organization"].pk),
            },
            format="json",
        )

        assert response.status_code == 201
        assert Invoice.objects.get(pk=response.data["id"]).organization_id == org.pk

    def test_another_organizations_customer_is_a_404(
        self, dispatcher_client, org, customer, service, rival
    ):
        job = make_job(org, customer, service)

        response = dispatcher_client.post(
            INVOICES_URL,
            {"customer": str(rival["customer"].pk), "jobs": [str(job.pk)]},
            format="json",
        )

        assert response.status_code == 404

    def test_another_organizations_visit_is_a_400(self, dispatcher_client, org, customer, rival):
        their_job = make_job(rival["organization"], rival["customer"], rival["service"])

        response = dispatcher_client.post(
            INVOICES_URL,
            {"customer": str(customer.pk), "jobs": [str(their_job.pk)]},
            format="json",
        )

        assert response.status_code == 400
        assert Invoice.objects.count() == 1  # only the rival's own

    def test_a_visit_already_invoiced_is_a_409(self, dispatcher_client, org, customer, service):
        job = make_job(org, customer, service)
        services.draft_invoice(customer=customer, jobs=[job])

        response = dispatcher_client.post(
            INVOICES_URL, {"customer": str(customer.pk), "jobs": [str(job.pk)]}, format="json"
        )

        assert response.status_code == 409
        assert "already on an invoice" in response.data["detail"]

    def test_an_unfinished_visit_is_a_400(self, dispatcher_client, org, customer, service):
        job = make_job(org, customer, service, status=JobStatus.SCHEDULED)

        response = dispatcher_client.post(
            INVOICES_URL, {"customer": str(customer.pk), "jobs": [str(job.pk)]}, format="json"
        )

        assert response.status_code == 400


# --- reading ---------------------------------------------------------------


@pytest.mark.django_db
class TestRead:
    def test_publishes_what_the_caller_may_do(self, dispatcher_client, issued):
        body = dispatcher_client.get(invoice_url(issued)).json()

        assert set(body["available_actions"]) == {"send", "record_payment", "void"}
        assert body["payment_state"] == "unpaid"
        assert body["balance_cents"] == issued.total_cents
        assert body["is_overdue"] is False

    def test_a_draft_shows_what_issuing_it_would_come_to(self, dispatcher_client, draft):
        body = dispatcher_client.get(invoice_url(draft)).json()

        assert body["total_cents"] == 15000
        assert body["number"] == ""
        assert "issue" in body["available_actions"]

    def test_lists_only_this_organizations(self, dispatcher_client, issued, rival):
        body = dispatcher_client.get(INVOICES_URL).json()

        assert body["count"] == 1
        assert body["results"][0]["id"] == str(issued.pk)

    def test_another_organizations_invoice_is_a_404_not_a_403(self, dispatcher_client, rival):
        """A 403 would confirm the record exists."""
        assert dispatcher_client.get(invoice_url(rival["invoice"])).status_code == 404

    def test_filters_by_status(self, dispatcher_client, draft, issued):
        body = dispatcher_client.get(f"{INVOICES_URL}?status=draft").json()

        assert [row["id"] for row in body["results"]] == [str(draft.pk)]

    def test_filters_by_payment_state(self, dispatcher_client, issued, org):
        services.record_payment(
            issued,
            method=PaymentMethod.CHECK,
            amount_cents=issued.total_cents,
            received_on=org.today(),
        )

        assert dispatcher_client.get(f"{INVOICES_URL}?payment_state=paid").json()["count"] == 1
        assert dispatcher_client.get(f"{INVOICES_URL}?payment_state=unpaid").json()["count"] == 0

    def test_filters_by_overdue(self, dispatcher_client, org, customer, service):
        invoice = services.draft_invoice(customer=customer, jobs=[make_job(org, customer, service)])
        services.issue_invoice(invoice, today=org.today() - dt.timedelta(days=90))

        assert dispatcher_client.get(f"{INVOICES_URL}?overdue=true").json()["count"] == 1
        assert dispatcher_client.get(f"{INVOICES_URL}?overdue=false").json()["count"] == 0


# --- editing and deleting --------------------------------------------------


@pytest.mark.django_db
class TestUpdateAndDelete:
    def test_a_drafts_notes_can_be_edited(self, dispatcher_client, draft):
        response = dispatcher_client.patch(
            invoice_url(draft), {"notes": "Thanks for a great year."}, format="json"
        )

        assert response.status_code == 200
        draft.refresh_from_db()
        assert draft.notes == "Thanks for a great year."

    def test_an_issued_invoice_cannot_be_edited(self, dispatcher_client, issued):
        response = dispatcher_client.patch(
            invoice_url(issued), {"notes": "Quietly changed"}, format="json"
        )

        assert response.status_code == 409
        issued.refresh_from_db()
        assert issued.notes == ""

    def test_the_snapshot_is_not_writable_even_on_a_draft(self, dispatcher_client, draft):
        response = dispatcher_client.patch(
            invoice_url(draft),
            {"total_cents": 1, "number": "INV-9999", "status": InvoiceStatus.ISSUED},
            format="json",
        )

        assert response.status_code == 200
        draft.refresh_from_db()
        assert draft.status == InvoiceStatus.DRAFT
        assert draft.number == ""
        assert draft.total_cents == 0  # still unwritten; the snapshot lands at issue

    def test_a_draft_can_be_thrown_away(self, dispatcher_client, draft):
        assert dispatcher_client.delete(invoice_url(draft)).status_code == 204
        assert Invoice.objects.filter(pk=draft.pk).count() == 0

    def test_an_issued_invoice_cannot_be_deleted(self, dispatcher_client, issued):
        response = dispatcher_client.delete(invoice_url(issued))

        assert response.status_code == 400
        assert Invoice.objects.filter(pk=issued.pk).exists()

    def test_another_organizations_invoice_cannot_be_edited(self, dispatcher_client, rival):
        response = dispatcher_client.patch(
            invoice_url(rival["invoice"]), {"notes": "theirs"}, format="json"
        )

        assert response.status_code == 404

    def test_another_organizations_invoice_cannot_be_deleted(self, dispatcher_client, rival):
        assert dispatcher_client.delete(invoice_url(rival["invoice"])).status_code == 404
        assert Invoice.objects.filter(pk=rival["invoice"].pk).exists()


# --- the actions -----------------------------------------------------------


@pytest.mark.django_db
class TestIssueAction:
    def test_numbers_and_freezes_it(self, dispatcher_client, draft):
        response = dispatcher_client.post(invoice_url(draft, "issue/"))

        assert response.status_code == 200
        assert response.data["number"] == "INV-0001"
        assert response.data["status"] == InvoiceStatus.ISSUED
        assert response.data["bill_to_name"] == "Dana Henderson"

    def test_issuing_twice_is_a_409_carrying_the_status(self, dispatcher_client, issued):
        response = dispatcher_client.post(invoice_url(issued, "issue/"))

        assert response.status_code == 409
        assert response.data["status"] == InvoiceStatus.ISSUED

    def test_an_empty_draft_is_a_409(self, dispatcher_client, org, customer):
        empty = InvoiceFactory(organization=org, customer=customer)

        assert dispatcher_client.post(invoice_url(empty, "issue/")).status_code == 409

    def test_another_organizations_draft_cannot_be_issued(self, dispatcher_client, rival):
        their_draft = services.draft_invoice(
            customer=rival["customer"],
            jobs=[make_job(rival["organization"], rival["customer"], rival["service"], days_ago=9)],
        )

        assert dispatcher_client.post(invoice_url(their_draft, "issue/")).status_code == 404
        their_draft.refresh_from_db()
        assert their_draft.status == InvoiceStatus.DRAFT


@pytest.mark.django_db
class TestVoidAction:
    def test_records_the_reason(self, dispatcher_client, issued, dispatcher):
        response = dispatcher_client.post(
            invoice_url(issued, "void/"), {"reason": "Billed the wrong address"}, format="json"
        )

        assert response.status_code == 200
        issued.refresh_from_db()
        assert issued.status == InvoiceStatus.VOID
        assert issued.voided_by == dispatcher

    def test_needs_a_reason(self, dispatcher_client, issued):
        response = dispatcher_client.post(invoice_url(issued, "void/"), {}, format="json")

        assert response.status_code == 400
        assert "reason" in response.data

    def test_is_a_409_while_money_points_at_it(self, dispatcher_client, issued, org):
        services.record_payment(
            issued,
            method=PaymentMethod.CASH,
            amount_cents=100,
            received_on=org.today(),
        )

        response = dispatcher_client.post(
            invoice_url(issued, "void/"), {"reason": "Mistake"}, format="json"
        )

        assert response.status_code == 409

    def test_another_organizations_invoice_cannot_be_voided(self, dispatcher_client, rival):
        response = dispatcher_client.post(
            invoice_url(rival["invoice"], "void/"), {"reason": "Mine now"}, format="json"
        )

        assert response.status_code == 404
        rival["invoice"].refresh_from_db()
        assert rival["invoice"].status == InvoiceStatus.ISSUED


@pytest.mark.django_db
class TestSendAction:
    def test_queues_the_email_and_answers_202(self, dispatcher_client, issued):
        from django.core import mail

        response = dispatcher_client.post(invoice_url(issued, "send/"))

        assert response.status_code == 202
        assert len(mail.outbox) == 1
        assert response.data["sent_at"] is not None

    def test_a_draft_cannot_be_sent(self, dispatcher_client, draft):
        assert dispatcher_client.post(invoice_url(draft, "send/")).status_code == 409

    def test_another_organizations_invoice_cannot_be_sent(self, dispatcher_client, rival):
        from django.core import mail

        assert dispatcher_client.post(invoice_url(rival["invoice"], "send/")).status_code == 404
        assert mail.outbox == []


# --- lines -----------------------------------------------------------------


@pytest.mark.django_db
class TestLines:
    def test_an_adjustment_can_be_added_to_a_draft(self, dispatcher_client, draft):
        response = dispatcher_client.post(
            LINES_URL,
            {
                "invoice": str(draft.pk),
                "kind": LineKind.ADJUSTMENT,
                "description": "Goodwill discount",
                "amount_cents": -2500,
            },
            format="json",
        )

        assert response.status_code == 201
        assert dispatcher_client.get(invoice_url(draft)).json()["total_cents"] == 12500

    def test_a_visit_line_cannot_be_written_by_hand(self, dispatcher_client, draft):
        response = dispatcher_client.post(
            LINES_URL,
            {
                "invoice": str(draft.pk),
                "kind": LineKind.VISIT,
                "description": "A visit I typed",
                "amount_cents": 99900,
            },
            format="json",
        )

        assert response.status_code == 400
        assert "kind" in response.data

    def test_nothing_can_be_added_to_an_issued_invoice(self, dispatcher_client, issued):
        response = dispatcher_client.post(
            LINES_URL,
            {
                "invoice": str(issued.pk),
                "kind": LineKind.ADJUSTMENT,
                "description": "Sneaky",
                "amount_cents": -100,
            },
            format="json",
        )

        assert response.status_code == 400

    def test_a_drafts_line_can_be_edited(self, dispatcher_client, draft):
        line = draft.lines.get()

        response = dispatcher_client.patch(line_url(line), {"amount_cents": 12000}, format="json")

        assert response.status_code == 200
        line.refresh_from_db()
        assert line.amount_cents == 12000

    def test_an_issued_invoices_line_cannot_be_edited(self, dispatcher_client, issued):
        line = issued.lines.get()

        response = dispatcher_client.patch(line_url(line), {"amount_cents": 1}, format="json")

        assert response.status_code == 409
        line.refresh_from_db()
        assert line.amount_cents == 15000

    def test_a_drafts_line_can_be_removed(self, dispatcher_client, draft):
        line = draft.lines.get()

        assert dispatcher_client.delete(line_url(line)).status_code == 204
        assert InvoiceLine.objects.filter(pk=line.pk).count() == 0

    def test_an_issued_invoices_line_cannot_be_removed(self, dispatcher_client, issued):
        line = issued.lines.get()

        assert dispatcher_client.delete(line_url(line)).status_code == 409
        assert InvoiceLine.objects.filter(pk=line.pk).exists()

    def test_a_line_cannot_be_added_to_another_organizations_invoice(
        self, dispatcher_client, rival
    ):
        """
        And the refusal says only "does not exist".

        `TenantModel.save()` would refuse the write regardless, but only after
        the serializer had already answered "that invoice is issued" -- which
        confirms both that the invoice exists and what state it is in. The
        invoice field is scoped to the caller's organization so that an id
        from anywhere else gets the same answer as an id from nowhere.
        """
        response = dispatcher_client.post(
            LINES_URL,
            {
                "invoice": str(rival["invoice"].pk),
                "kind": LineKind.ADJUSTMENT,
                "description": "Mine now",
                "amount_cents": -100,
            },
            format="json",
        )

        assert response.status_code == 400
        assert "does not exist" in str(response.data["invoice"])
        assert "organization" not in str(response.data["invoice"]).lower()
        assert not InvoiceLine.objects.filter(description="Mine now").exists()

    def test_an_unknown_invoice_id_says_the_same_thing(self, dispatcher_client):
        import uuid

        response = dispatcher_client.post(
            LINES_URL,
            {
                "invoice": str(uuid.uuid4()),
                "kind": LineKind.ADJUSTMENT,
                "description": "Nowhere",
                "amount_cents": -100,
            },
            format="json",
        )

        assert response.status_code == 400
        assert "does not exist" in str(response.data["invoice"])

    def test_lists_only_this_organizations(self, dispatcher_client, draft, rival):
        body = dispatcher_client.get(LINES_URL).json()

        assert {row["id"] for row in body["results"]} == {str(draft.lines.get().pk)}

    def test_another_organizations_line_cannot_be_edited(self, dispatcher_client, rival):
        their_line = rival["invoice"].lines.get()

        response = dispatcher_client.patch(line_url(their_line), {"amount_cents": 1}, format="json")

        assert response.status_code == 404

    def test_another_organizations_line_cannot_be_removed(self, dispatcher_client, rival):
        their_line = rival["invoice"].lines.get()

        assert dispatcher_client.delete(line_url(their_line)).status_code == 404
        assert InvoiceLine.objects.filter(pk=their_line.pk).exists()

    def test_another_organizations_line_cannot_be_repriced(self, dispatcher_client, rival):
        their_line = rival["invoice"].lines.get()

        assert dispatcher_client.post(line_url(their_line, "reprice/")).status_code == 404


# --- payments --------------------------------------------------------------


@pytest.mark.django_db
class TestPaymentsApi:
    def test_records_a_check_against_an_invoice(self, dispatcher_client, issued, dispatcher, org):
        response = dispatcher_client.post(
            PAYMENTS_URL,
            {
                "invoice": str(issued.pk),
                "method": PaymentMethod.CHECK,
                "amount_cents": 15000,
                "received_on": str(org.today()),
                "reference": "Check 401",
            },
            format="json",
        )

        assert response.status_code == 201
        assert str(response.data["recorded_by"]) == str(dispatcher.pk)
        assert dispatcher_client.get(invoice_url(issued)).json()["payment_state"] == "paid"

    def test_more_than_the_balance_is_a_400_naming_the_field(self, dispatcher_client, issued, org):
        response = dispatcher_client.post(
            PAYMENTS_URL,
            {
                "invoice": str(issued.pk),
                "method": PaymentMethod.CASH,
                "amount_cents": 15001,
                "received_on": str(org.today()),
            },
            format="json",
        )

        assert response.status_code == 400
        assert "amount_cents" in response.data

    def test_paying_a_draft_is_a_409(self, dispatcher_client, draft, org):
        response = dispatcher_client.post(
            PAYMENTS_URL,
            {
                "invoice": str(draft.pk),
                "method": PaymentMethod.CASH,
                "amount_cents": 100,
                "received_on": str(org.today()),
            },
            format="json",
        )

        assert response.status_code == 409

    def test_a_tip_rides_alongside(self, dispatcher_client, issued, org):
        response = dispatcher_client.post(
            PAYMENTS_URL,
            {
                "invoice": str(issued.pk),
                "method": PaymentMethod.CASH,
                "amount_cents": 5000,
                "tip_cents": 2000,
                "received_on": str(org.today()),
            },
            format="json",
        )

        assert response.status_code == 201
        body = dispatcher_client.get(invoice_url(issued)).json()
        assert body["balance_cents"] == 10000

    def test_cannot_pay_another_organizations_invoice(self, dispatcher_client, rival, org):
        response = dispatcher_client.post(
            PAYMENTS_URL,
            {
                "invoice": str(rival["invoice"].pk),
                "method": PaymentMethod.CASH,
                "amount_cents": 100,
                "received_on": str(org.today()),
            },
            format="json",
        )

        assert response.status_code == 404
        assert Payment.objects.count() == 0

    def test_voiding_gives_the_balance_back(self, dispatcher_client, issued, org):
        payment = services.record_payment(
            issued, method=PaymentMethod.CHECK, amount_cents=15000, received_on=org.today()
        )

        response = dispatcher_client.post(
            payment_url(payment, "void/"), {"reason": "Check bounced"}, format="json"
        )

        assert response.status_code == 200
        assert dispatcher_client.get(invoice_url(issued)).json()["balance_cents"] == 15000

    def test_voiding_needs_a_reason(self, dispatcher_client, issued, org):
        payment = services.record_payment(
            issued, method=PaymentMethod.CASH, amount_cents=100, received_on=org.today()
        )

        response = dispatcher_client.post(payment_url(payment, "void/"), {}, format="json")

        assert response.status_code == 400

    def test_there_is_no_patch_and_no_delete(self, dispatcher_client, issued, org):
        payment = services.record_payment(
            issued, method=PaymentMethod.CASH, amount_cents=100, received_on=org.today()
        )

        assert (
            dispatcher_client.patch(
                payment_url(payment), {"amount_cents": 1}, format="json"
            ).status_code
            == 405
        )
        assert dispatcher_client.delete(payment_url(payment)).status_code == 405

    def test_lists_only_this_organizations(self, dispatcher_client, issued, rival, org):
        services.record_payment(
            issued, method=PaymentMethod.CASH, amount_cents=100, received_on=org.today()
        )
        services.record_payment(
            rival["invoice"],
            method=PaymentMethod.CASH,
            amount_cents=100,
            received_on=rival["organization"].today(),
        )

        body = dispatcher_client.get(PAYMENTS_URL).json()

        assert body["count"] == 1
        assert body["results"][0]["invoice"] == str(issued.pk)

    def test_another_organizations_payment_cannot_be_voided(self, dispatcher_client, rival):
        theirs = services.record_payment(
            rival["invoice"],
            method=PaymentMethod.CASH,
            amount_cents=100,
            received_on=rival["organization"].today(),
        )

        response = dispatcher_client.post(
            payment_url(theirs, "void/"), {"reason": "Mine now"}, format="json"
        )

        assert response.status_code == 404
        theirs.refresh_from_db()
        assert theirs.voided_at is None


# --- who may reach any of this --------------------------------------------


@pytest.mark.django_db
class TestPermissions:
    """Dispatcher and above. A cleaner and a customer get 403 everywhere."""

    @pytest.fixture
    def cleaner(self, org):
        user = UserFactory()
        MembershipFactory(user=user, organization=org, role=Role.CLEANER)
        return user

    @pytest.fixture
    def portal_customer(self, org, customer):
        user = UserFactory()
        MembershipFactory(user=user, organization=org, role=Role.CUSTOMER)
        customer.user = user
        customer.save()
        return user

    @pytest.mark.parametrize("role", [Role.OWNER, Role.ADMIN, Role.DISPATCHER])
    def test_dispatcher_and_above_may_read(self, client_as, org, issued, role):
        user = UserFactory()
        MembershipFactory(user=user, organization=org, role=role)

        assert client_as(user).get(INVOICES_URL).status_code == 200

    def test_a_cleaner_may_not_read(self, client_as, cleaner, issued):
        assert client_as(cleaner).get(INVOICES_URL).status_code == 403

    def test_a_customer_may_not_read_even_their_own(self, client_as, portal_customer, issued):
        """Their own view of their invoices is Phase 5, and a narrower one."""
        assert client_as(portal_customer).get(invoice_url(issued)).status_code == 403

    def test_a_cleaner_may_not_reach_the_ready_to_invoice_list(self, client_as, cleaner):
        assert client_as(cleaner).get(BILLABLE_URL).status_code == 403

    def test_a_cleaner_may_not_create_an_invoice(self, client_as, cleaner, org, customer, service):
        job = make_job(org, customer, service)

        response = client_as(cleaner).post(
            INVOICES_URL, {"customer": str(customer.pk), "jobs": [str(job.pk)]}, format="json"
        )

        assert response.status_code == 403
        assert Invoice.objects.count() == 0

    def test_a_cleaner_may_not_issue(self, client_as, cleaner, draft):
        assert client_as(cleaner).post(invoice_url(draft, "issue/")).status_code == 403
        draft.refresh_from_db()
        assert draft.status == InvoiceStatus.DRAFT

    def test_a_cleaner_may_not_void(self, client_as, cleaner, issued):
        response = client_as(cleaner).post(
            invoice_url(issued, "void/"), {"reason": "no"}, format="json"
        )

        assert response.status_code == 403

    def test_a_cleaner_may_not_delete_a_draft(self, client_as, cleaner, draft):
        assert client_as(cleaner).delete(invoice_url(draft)).status_code == 403
        assert Invoice.objects.filter(pk=draft.pk).exists()

    def test_a_cleaner_may_not_touch_lines(self, client_as, cleaner, draft):
        line = draft.lines.get()
        signed_in = client_as(cleaner)

        assert signed_in.get(LINES_URL).status_code == 403
        assert (
            signed_in.patch(line_url(line), {"amount_cents": 1}, format="json").status_code == 403
        )
        assert signed_in.delete(line_url(line)).status_code == 403
        assert signed_in.post(line_url(line, "reprice/")).status_code == 403

    def test_a_cleaner_may_not_record_or_void_a_payment(self, client_as, cleaner, issued, org):
        signed_in = client_as(cleaner)
        payment = services.record_payment(
            issued, method=PaymentMethod.CASH, amount_cents=100, received_on=org.today()
        )

        assert signed_in.get(PAYMENTS_URL).status_code == 403
        assert (
            signed_in.post(
                PAYMENTS_URL,
                {
                    "invoice": str(issued.pk),
                    "method": PaymentMethod.CASH,
                    "amount_cents": 100,
                    "received_on": str(org.today()),
                },
                format="json",
            ).status_code
            == 403
        )
        assert (
            signed_in.post(
                payment_url(payment, "void/"), {"reason": "no"}, format="json"
            ).status_code
            == 403
        )

    def test_signed_out_is_a_403(self, api_client, issued):
        assert api_client.get(INVOICES_URL).status_code == 403

    def test_a_deactivated_organization_is_read_only(self, dispatcher_client, org, draft):
        org.is_active = False
        org.save()

        assert dispatcher_client.get(INVOICES_URL).status_code == 200
        assert dispatcher_client.post(invoice_url(draft, "issue/")).status_code == 403
