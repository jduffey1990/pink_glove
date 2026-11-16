"""
A visit on an invoice is not reopened from under it (ADR-033).

The reproduction that started this: issue an invoice over a complete visit,
reopen the visit through the status action, delete it. Each step answered
2xx, and the invoice was left billing a visit that no longer existed.

The rule is billing's and the status action is scheduling's, and `scheduling`
may not import `billing`. So billing registers a check with scheduling at
startup (`BillingConfig.ready`), and these tests drive the whole thing through
HTTP, where the two meet. The mechanism on its own, with a stand-in check, is
tested in `scheduling/tests/test_claim_guard.py`.
"""

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from billing.enums import InvoiceStatus, LineKind
from billing.models import Invoice, InvoiceLine
from billing.tests.conftest import completed_job, staff_in
from organizations.enums import NoAccessFeeType
from scheduling.enums import JobStatus
from scheduling.models import Job, JobAssignment
from scheduling.tests.factories import CustomerFactory, OrganizationFactory
from users.enums import Role

INVOICES = reverse("billing:invoice-list")
JOBS = reverse("scheduling:job-list")


def job_url(job):
    return reverse("scheduling:job-detail", args=[job.id])


def reopen(client, job):
    return client.post(
        reverse("scheduling:job-status", args=[job.id]), {"status": "scheduled"}, format="json"
    )


def draft_over(client, customer, *jobs) -> str:
    response = client.post(
        INVOICES,
        {"customer": str(customer.id), "jobs": [str(job.id) for job in jobs]},
        format="json",
    )
    assert response.status_code == 201, response.json()
    return response.json()["id"]


def issue(client, invoice_id) -> dict:
    response = client.post(reverse("billing:invoice-issue", args=[invoice_id]))
    assert response.status_code == 200, response.json()
    return response.json()


def void(client, invoice_id):
    response = client.post(
        reverse("billing:invoice-void", args=[invoice_id]),
        {"reason": "Billed the wrong week."},
        format="json",
    )
    assert response.status_code == 200, response.json()


@pytest.fixture
def visit(org, customer, service):
    return completed_job(org, customer, service)


@pytest.fixture
def issued(dispatcher_client, customer, visit) -> dict:
    """An issued invoice over `visit`, made the way a dispatcher makes one."""
    return issue(dispatcher_client, draft_over(dispatcher_client, customer, visit))


def claimed(client, customer, job, how):
    """Put `job` in one of the four billing states a finished visit can be in."""
    if how == "unbilled":
        return
    invoice_id = draft_over(client, customer, job)
    if how in ("issued", "voided"):
        issue(client, invoice_id)
    if how == "voided":
        void(client, invoice_id)


#: Whether a visit in each billing state may be reopened.
CLAIMS = {"unbilled": False, "draft": True, "issued": True, "voided": False}


@pytest.mark.django_db
class TestTheReproduction:
    def test_an_invoiced_visit_cannot_be_reopened_and_then_deleted(
        self, dispatcher_client, visit, issued
    ):
        reopened = reopen(dispatcher_client, visit)
        deleted = dispatcher_client.delete(job_url(visit))

        visit.refresh_from_db()
        assert reopened.status_code == 409
        assert deleted.status_code == 409
        assert visit.status == JobStatus.COMPLETE
        assert visit.deleted_at is None
        # The invoice still has the visit it was issued for.
        assert InvoiceLine.objects.get(invoice_id=issued["id"]).job_id == visit.id


@pytest.mark.django_db
class TestReopeningABilledVisit:
    def test_the_refusal_names_the_invoice_and_says_what_to_do(
        self, dispatcher_client, visit, issued
    ):
        body = reopen(dispatcher_client, visit).json()

        assert issued["number"] in body["detail"]
        assert "void" in body["detail"].lower()
        assert body["status"] == "complete"
        assert body["allowed"] == []

    def test_voiding_the_invoice_frees_the_visit(self, dispatcher_client, visit, issued):
        void(dispatcher_client, issued["id"])

        response = reopen(dispatcher_client, visit)

        assert response.status_code == 200
        assert response.json()["status"] == "scheduled"

    def test_a_visit_on_no_invoice_reopens_as_it_always_did(self, dispatcher_client, visit):
        assert reopen(dispatcher_client, visit).status_code == 200

    def test_a_no_access_visit_that_was_charged_for_is_held_the_same_way(
        self, dispatcher_client, org, customer, service
    ):
        org.no_access_fee_type = NoAccessFeeType.FLAT
        org.no_access_fee_value = 2500
        org.save()
        locked_out = completed_job(org, customer, service, status=JobStatus.NO_ACCESS)
        issue(dispatcher_client, draft_over(dispatcher_client, customer, locked_out))

        assert reopen(dispatcher_client, locked_out).status_code == 409

    def test_a_cleaner_is_told_what_a_cleaner_was_always_told(self, client_as, org, visit, issued):
        """Reopening is a dispatcher's move. Billing's sentence is not theirs to act on."""
        cleaner = staff_in(org, Role.CLEANER)
        JobAssignment.objects.create(organization=org, job=visit, user=cleaner)

        body = reopen(client_as(cleaner), visit).json()

        assert "dispatcher" in body["detail"]
        assert issued["number"] not in body["detail"]


@pytest.mark.django_db
class TestADraftHoldsAVisitToo:
    """
    A draft is not a sent document, but it is issued as it stands: nothing at
    issue checks that its visits are still finished. Reopening a visit on a
    draft left a draft that would bill a visit nobody had done, and kept the
    visit off the ready-to-invoice list once it was done again. The remedy is
    one click, so the draft refuses.
    """

    def test_a_visit_on_a_draft_cannot_be_reopened(self, dispatcher_client, customer, visit):
        draft_over(dispatcher_client, customer, visit)

        body = reopen(dispatcher_client, visit)

        visit.refresh_from_db()
        assert body.status_code == 409
        assert "draft" in body.json()["detail"].lower()
        assert visit.status == JobStatus.COMPLETE

    def test_taking_it_off_the_draft_frees_it(
        self, dispatcher_client, customer, visit, org, service
    ):
        other = completed_job(org, customer, service, days_ago=2)
        invoice_id = draft_over(dispatcher_client, customer, visit, other)
        line = InvoiceLine.objects.get(invoice_id=invoice_id, job=visit)

        dispatcher_client.delete(reverse("billing:invoiceline-detail", args=[line.id]))

        assert reopen(dispatcher_client, visit).status_code == 200
        assert reopen(dispatcher_client, other).status_code == 409

    def test_deleting_the_draft_frees_it(self, dispatcher_client, customer, visit):
        invoice_id = draft_over(dispatcher_client, customer, visit)

        dispatcher_client.delete(reverse("billing:invoice-detail", args=[invoice_id]))

        assert reopen(dispatcher_client, visit).status_code == 200


@pytest.mark.django_db
class TestPublishedIsWhatIsAccepted:
    @pytest.mark.parametrize("how", CLAIMS)
    @pytest.mark.parametrize("status", [JobStatus.COMPLETE, JobStatus.NO_ACCESS])
    def test_reopen_is_offered_exactly_when_it_will_work(
        self, dispatcher_client, org, customer, service, how, status
    ):
        org.no_access_fee_type = NoAccessFeeType.FLAT
        org.no_access_fee_value = 2500
        org.save()
        job = completed_job(org, customer, service, status=status)
        claimed(dispatcher_client, customer, job, how)

        offered = dispatcher_client.get(job_url(job)).json()["next_statuses"]
        response = reopen(dispatcher_client, job)

        assert (offered != []) == (not CLAIMS[how])
        assert (response.status_code == 200) == (offered != [])
        assert response.status_code in (200, 409)

    @pytest.mark.parametrize("how", CLAIMS)
    @pytest.mark.parametrize("status", JobStatus.values)
    def test_edit_and_delete_are_offered_exactly_when_they_will_work(
        self, dispatcher_client, org, customer, service, how, status
    ):
        """
        Every status, not only the finished ones. A billed visit that is
        somehow not finished (reopened before this guard existed) must not be
        editable or deletable either, and must not say it is.
        """
        job = completed_job(org, customer, service)
        claimed(dispatcher_client, customer, job, how)
        Job.objects.filter(pk=job.pk).update(status=status)

        offered = dispatcher_client.get(job_url(job)).json()["available_actions"]
        edit = dispatcher_client.patch(job_url(job), {"notes": "Checked."}, format="json")
        delete = dispatcher_client.delete(job_url(job))

        if CLAIMS[how]:
            assert offered == []
        assert (edit.status_code == 200) == ("edit" in offered)
        assert (delete.status_code == 204) == ("delete" in offered)
        assert edit.status_code in (200, 409)
        assert delete.status_code in (204, 409)

    def test_a_billed_visit_that_was_already_reopened_says_why_it_is_stuck(
        self, dispatcher_client, visit, issued
    ):
        Job.objects.filter(pk=visit.pk).update(status=JobStatus.SCHEDULED)

        edit = dispatcher_client.patch(job_url(visit), {"price_cents": 1}, format="json")
        delete = dispatcher_client.delete(job_url(visit))

        assert edit.status_code == delete.status_code == 409
        assert issued["number"] in edit.json()["detail"]
        assert issued["number"] in delete.json()["detail"]

    def test_the_response_to_voiding_is_followed_by_an_offer_to_reopen(
        self, dispatcher_client, visit, issued
    ):
        assert dispatcher_client.get(job_url(visit)).json()["next_statuses"] == []

        void(dispatcher_client, issued["id"])

        offered = dispatcher_client.get(job_url(visit)).json()["next_statuses"]
        assert [move["status"] for move in offered] == ["scheduled"]


@pytest.mark.django_db
class TestTheJobList:
    def test_each_row_carries_its_own_answer(self, dispatcher_client, org, customer, service):
        billed = completed_job(org, customer, service, days_ago=3)
        free = completed_job(org, customer, service, days_ago=2)
        issue(dispatcher_client, draft_over(dispatcher_client, customer, billed))

        rows = {row["id"]: row for row in dispatcher_client.get(JOBS).json()["results"]}

        assert rows[str(billed.id)]["next_statuses"] == []
        assert [m["status"] for m in rows[str(free.id)]["next_statuses"]] == ["scheduled"]

    def test_the_guard_costs_one_query_for_the_page_not_one_per_row(
        self, dispatcher_client, org, customer, service
    ):
        def queries_for_list():
            with CaptureQueriesContext(connection) as captured:
                assert dispatcher_client.get(JOBS).status_code == 200
            return len(captured)

        def a_billed_visit(days_ago):
            job = completed_job(org, customer, service, days_ago=days_ago)
            issue(dispatcher_client, draft_over(dispatcher_client, customer, job))

        a_billed_visit(1)
        completed_job(org, customer, service, days_ago=2)
        with_two = queries_for_list()

        for days_ago in range(3, 9):
            a_billed_visit(days_ago)
            completed_job(org, customer, service, days_ago=days_ago)

        assert queries_for_list() == with_two


@pytest.mark.django_db
class TestTenancy:
    def test_another_organizations_invoice_never_holds_this_ones_visit(
        self, dispatcher_client, visit
    ):
        """
        `TenantModel.save()` already refuses a line that points across
        organizations, so this row is forced in underneath it. The guard must
        not take another tenant's line as a claim even then.
        """
        elsewhere = OrganizationFactory()
        their_invoice = Invoice.objects.create(
            organization=elsewhere,
            customer=CustomerFactory(organization=elsewhere),
            status=InvoiceStatus.ISSUED,
            number="RIVAL-0001",
        )
        InvoiceLine.objects.bulk_create(
            [
                InvoiceLine(
                    organization=elsewhere,
                    invoice=their_invoice,
                    job=visit,
                    kind=LineKind.VISIT,
                    description="Not theirs to bill",
                    amount_cents=15000,
                )
            ]
        )

        response = reopen(dispatcher_client, visit)

        assert response.status_code == 200

    def test_the_refusal_is_about_this_organizations_invoice_only(
        self, dispatcher_client, client_as, visit, issued
    ):
        """Their own invoiced visit is held; ours is unaffected by it and they cannot see ours."""
        elsewhere = OrganizationFactory()
        their_customer = CustomerFactory(organization=elsewhere)
        their_visit = completed_job(elsewhere, their_customer)
        their_dispatcher = client_as(staff_in(elsewhere, Role.DISPATCHER))

        assert reopen(their_dispatcher, visit).status_code == 404
        assert reopen(their_dispatcher, their_visit).status_code == 200
