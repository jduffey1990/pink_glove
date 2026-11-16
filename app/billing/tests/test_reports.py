"""
The money reports: receivables and collections.

Two things a money report gets wrong quietly, and so are tested hardest: it
disagrees with the invoice page (a second balance formula), and it counts
something from another tenant. Every balance here is checked against
`services.balance_cents`, and the two organizations in the isolation tests
bill differently on purpose -- one charges tax, the other does not -- so a
row read from the wrong tenant is a wrong number, not a plausible one.

ADR-030 records what each report means; these pin it.
"""

import csv
import datetime as dt
import io
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from billing import reports, services
from billing.enums import InvoiceStatus, PaymentMethod
from billing.models import Invoice, Payment
from billing.tests.conftest import completed_job, staff_in
from billing.tests.factories import (
    InvoiceFactory,
    InvoiceLineFactory,
    IssuedInvoiceFactory,
    PaymentFactory,
)
from organizations.enums import NoAccessFeeType
from organizations.models import Organization
from scheduling.tests.factories import (
    CustomerFactory,
    MembershipFactory,
    OrganizationFactory,
    ServiceFactory,
)
from users.enums import Role

RECEIVABLES = reverse("billing:report-receivables")
RECEIVABLES_CSV = reverse("billing:report-receivables-csv")
COLLECTIONS = reverse("billing:report-collections")
COLLECTIONS_CSV = reverse("billing:report-collections-csv")

#: The date every aging test counts from. Passed explicitly, so no test
#: depends on what day it is run.
AS_OF = dt.date(2027, 6, 30)
JUNE = {"date_from": "2027-06-01", "date_to": "2027-06-30"}

#: Each report with a query that is valid for it.
ALL_REPORTS = [
    (RECEIVABLES, {"as_of": "2027-06-30"}),
    (RECEIVABLES_CSV, {"as_of": "2027-06-30"}),
    (COLLECTIONS, JUNE),
    (COLLECTIONS_CSV, JUNE),
]


def client_for(user) -> APIClient:
    """A client of its own, so two tenants can be signed in side by side."""
    client = APIClient()
    client.force_login(user)
    return client


def read_csv(response) -> list[list[str]]:
    return list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))


def owed(org, *, total_cents=15000, days_past_due=0, customer=None):
    """An issued invoice for `total_cents`, due `days_past_due` days before AS_OF."""
    return IssuedInvoiceFactory(
        organization=org,
        customer=customer or CustomerFactory(organization=org),
        subtotal_cents=total_cents,
        total_cents=total_cents,
        due_on=AS_OF - dt.timedelta(days=days_past_due),
    )


def paid(invoice, amount_cents, **extra):
    extra.setdefault("received_on", dt.date(2027, 6, 15))
    return PaymentFactory(
        organization=invoice.organization, invoice=invoice, amount_cents=amount_cents, **extra
    )


@pytest.fixture
def owner(org):
    return staff_in(org, Role.OWNER)


@pytest.fixture
def owner_client(owner):
    return client_for(owner)


def receivables(client, **query):
    query.setdefault("as_of", AS_OF.isoformat())
    response = client.get(RECEIVABLES, query)
    assert response.status_code == 200, response.content
    return response.json()


def collections(client, **query):
    response = client.get(COLLECTIONS, {**JUNE, **query})
    assert response.status_code == 200, response.content
    return response.json()


# --- who may read them -----------------------------------------------------


@pytest.mark.django_db
class TestPermissions:
    @pytest.mark.parametrize(("url", "query"), ALL_REPORTS)
    @pytest.mark.parametrize("role", [Role.OWNER, Role.ADMIN])
    def test_owner_and_admin_may_read_it(self, url, query, role, org):
        assert client_for(staff_in(org, role)).get(url, query).status_code == 200

    @pytest.mark.parametrize(("url", "query"), ALL_REPORTS)
    @pytest.mark.parametrize("role", [Role.DISPATCHER, Role.CLEANER, Role.CUSTOMER])
    def test_everyone_else_is_refused(self, url, query, role, org):
        """
        A dispatcher included, though the rest of billing is theirs: one
        invoice is their work, the whole position is not.
        """
        assert client_for(staff_in(org, role)).get(url, query).status_code == 403

    @pytest.mark.parametrize(("url", "query"), ALL_REPORTS)
    def test_nobody_signed_in_is_refused(self, url, query, api_client):
        assert api_client.get(url, query).status_code == 403

    @pytest.mark.parametrize(("url", "query"), ALL_REPORTS)
    def test_the_role_that_counts_is_the_one_held_in_the_organization_asked_for(
        self, url, query, org
    ):
        """
        An owner here who also cleans for someone else is a cleaner there.
        Naming the other organization in the header must not carry the
        ownership across with it.
        """
        elsewhere = OrganizationFactory()
        person = staff_in(org, Role.OWNER)
        MembershipFactory(user=person, organization=elsewhere, role=Role.CLEANER)
        client = client_for(person)

        here = client.get(url, query, headers={"X-Organization": str(org.pk)})
        there = client.get(url, query, headers={"X-Organization": str(elsewhere.pk)})

        assert here.status_code == 200
        assert there.status_code == 403

    @pytest.mark.parametrize(("url", "query"), ALL_REPORTS)
    def test_a_superuser_reads_one_organization_at_a_time(self, url, query, org, superuser):
        """Platform administration, never a way round the tenant: no header, no report."""
        client = client_for(superuser)

        assert client.get(url, query).status_code == 403
        assert client.get(url, query, headers={"X-Organization": str(org.pk)}).status_code == 200

    @pytest.mark.parametrize(("url", "query"), ALL_REPORTS)
    def test_a_deactivated_organization_can_still_read_them(self, url, query, org):
        """
        Suspended is read-only, not locked out: a tenant behind on its
        subscription still has to be able to see who owes it money.
        """
        org.is_active = False
        org.save()

        assert client_for(staff_in(org, Role.OWNER)).get(url, query).status_code == 200


# --- tenant isolation ------------------------------------------------------


@pytest.mark.django_db
class TestTenantIsolation:
    """
    Two organizations, built through the real billing services, with the
    same $150.00 visit each. One charges 8.25% tax and a flat no-access fee;
    the other charges neither. So they are owed, and collect, different
    amounts for the same work.
    """

    @pytest.fixture
    def taxed(self):
        return OrganizationFactory(
            timezone="America/Denver",
            tax_rate_percent=Decimal("8.250"),
            no_access_fee_type=NoAccessFeeType.FLAT,
            no_access_fee_value=Decimal("2500"),
        )

    @pytest.fixture
    def untaxed(self):
        return OrganizationFactory(timezone="America/New_York")

    def _bill(self, org):
        customer = CustomerFactory(organization=org, first_name="Dana", last_name=org.slug)
        service = ServiceFactory(organization=org, is_taxable=True)
        invoice = services.draft_invoice(
            customer=customer, jobs=[completed_job(org, customer, service, price_cents=15000)]
        )
        services.issue_invoice(invoice, today=dt.date(2027, 6, 1))
        services.record_payment(
            invoice,
            method=PaymentMethod.CHECK,
            amount_cents=5000,
            received_on=dt.date(2027, 6, 10),
        )
        return invoice

    @pytest.fixture
    def both_billed(self, taxed, untaxed):
        return self._bill(taxed), self._bill(untaxed)

    def test_receivables_hold_only_their_own_organization(self, both_billed, taxed, untaxed):
        ours, theirs = both_billed

        mine = receivables(client_for(staff_in(taxed, Role.OWNER)))
        rivals = receivables(client_for(staff_in(untaxed, Role.OWNER)))

        # $150.00 + 8.25% = $162.38, less the $50.00 paid.
        assert mine["totals"]["total_cents"] == 11238
        assert [row["customer"] for row in mine["customers"]] == [str(ours.customer_id)]
        # No tax: $150.00 less $50.00.
        assert rivals["totals"]["total_cents"] == 10000
        assert [row["customer"] for row in rivals["customers"]] == [str(theirs.customer_id)]

    def test_collections_hold_only_their_own_organization(self, both_billed, taxed, untaxed):
        paid(both_billed[1], 2500, method=PaymentMethod.CASH)

        mine = collections(client_for(staff_in(taxed, Role.OWNER)))
        rivals = collections(client_for(staff_in(untaxed, Role.OWNER)))

        assert mine["totals"]["amount_cents"] == 5000
        assert [row["method"] for row in mine["methods"]] == ["check"]
        assert rivals["totals"]["amount_cents"] == 7500
        assert [row["method"] for row in rivals["methods"]] == ["cash", "check"]

    @pytest.mark.parametrize(
        ("url", "query"), [(RECEIVABLES_CSV, {"as_of": "2027-06-30"}), (COLLECTIONS_CSV, JUNE)]
    )
    def test_the_csvs_hold_only_their_own_organization(
        self, url, query, both_billed, taxed, untaxed
    ):
        mine = read_csv(client_for(staff_in(taxed, Role.OWNER)).get(url, query))
        rivals = read_csv(client_for(staff_in(untaxed, Role.OWNER)).get(url, query))

        assert len(mine) == len(rivals) == 2
        # The last money column differs: the tax is in one and not the other,
        # or the same check is the whole of one tenant's receipts.
        assert f"Dana {untaxed.slug}" not in mine[1][0]
        assert f"Dana {taxed.slug}" not in rivals[1][0]
        if url == RECEIVABLES_CSV:
            assert (mine[1][-1], rivals[1][-1]) == ("112.38", "100.00")


# --- receivables: what counts ----------------------------------------------


@pytest.mark.django_db
class TestReceivablesWhatCounts:
    def test_an_unpaid_issued_invoice_is_owed_in_full(self, org, owner_client):
        invoice = owed(org, total_cents=15000)

        report = receivables(owner_client)

        assert report["as_of"] == "2027-06-30"
        assert report["customers"] == [
            {
                "customer": str(invoice.customer_id),
                "customer_name": invoice.customer.display_name,
                "invoice_count": 1,
                "not_due_cents": 15000,
                "days_1_30_cents": 0,
                "days_31_60_cents": 0,
                "days_61_90_cents": 0,
                "days_over_90_cents": 0,
                "total_cents": 15000,
            }
        ]

    def test_a_part_payment_leaves_the_rest(self, org, owner_client):
        paid(owed(org, total_cents=15000), 6000)

        assert receivables(owner_client)["totals"]["total_cents"] == 9000

    def test_a_tip_settles_nothing(self, org, owner_client):
        paid(owed(org, total_cents=15000), 6000, tip_cents=2000)

        assert receivables(owner_client)["totals"]["total_cents"] == 9000

    def test_a_settled_invoice_is_not_listed(self, org, owner_client):
        paid(owed(org, total_cents=15000), 15000)

        report = receivables(owner_client)

        assert report["customers"] == []
        assert report["totals"]["total_cents"] == 0
        assert report["totals"]["invoice_count"] == 0

    def test_a_voided_payment_gives_the_balance_back(self, org, owner_client):
        payment = paid(owed(org, total_cents=15000), 15000)
        services.void_payment(payment, reason="Check bounced")

        assert receivables(owner_client)["totals"]["total_cents"] == 15000

    def test_a_draft_is_not_a_debt(self, org, owner_client):
        InvoiceFactory(organization=org, total_cents=15000)

        assert receivables(owner_client)["customers"] == []

    def test_a_void_invoice_is_not_a_debt(self, org, owner_client):
        invoice = owed(org, total_cents=15000)
        services.void_invoice(invoice, reason="Billed the wrong visits")

        assert receivables(owner_client)["customers"] == []

    def test_an_overpayment_is_not_a_negative_debt(self, org, owner_client):
        """Only a provider can overpay; the excess is a refund, not a credit here."""
        invoice = owed(org, total_cents=15000)
        paid(invoice, 15000)
        paid(invoice, 4000, provider="stripe", provider_reference="pi_over", recorded_by=None)

        report = receivables(owner_client)

        assert report["customers"] == []
        assert report["totals"]["total_cents"] == 0

    def test_a_customer_with_several_invoices_is_one_row(self, org, owner_client):
        customer = CustomerFactory(organization=org)
        owed(org, total_cents=10000, days_past_due=0, customer=customer)
        owed(org, total_cents=20000, days_past_due=45, customer=customer)
        paid(owed(org, total_cents=30000, days_past_due=45, customer=customer), 5000)

        (row,) = receivables(owner_client)["customers"]

        assert row["invoice_count"] == 3
        assert row["not_due_cents"] == 10000
        assert row["days_31_60_cents"] == 45000
        assert row["total_cents"] == 55000

    def test_totals_are_the_sum_of_the_rows(self, org, owner_client):
        owed(org, total_cents=10000, days_past_due=0)
        owed(org, total_cents=20000, days_past_due=10)
        owed(org, total_cents=30000, days_past_due=100)

        report = receivables(owner_client)

        assert report["totals"] == {
            "invoice_count": 3,
            "not_due_cents": 10000,
            "days_1_30_cents": 20000,
            "days_31_60_cents": 0,
            "days_61_90_cents": 0,
            "days_over_90_cents": 30000,
            "total_cents": 60000,
        }


@pytest.mark.django_db
class TestReceivablesAgreeWithTheLedger:
    """
    The report has no balance formula of its own. These hold it to that: for
    a ledger with every awkward case in it, what the report says is owed is
    what `services.balance_cents` says, invoice by invoice.
    """

    @pytest.fixture
    def awkward_ledger(self, org):
        untouched = owed(org, total_cents=12345)
        part_paid = owed(org, total_cents=20000, days_past_due=40)
        paid(part_paid, 7000, tip_cents=1500)
        paid(part_paid, 3000)
        bounced = owed(org, total_cents=9000, days_past_due=5)
        services.void_payment(paid(bounced, 9000), reason="Bounced")
        settled = owed(org, total_cents=5000)
        paid(settled, 5000)
        overpaid = owed(org, total_cents=5000)
        paid(overpaid, 5000)
        paid(overpaid, 100, provider="stripe", provider_reference="pi_extra", recorded_by=None)
        # A payment row soft-deleted behind the model's back (the admin can).
        erased = owed(org, total_cents=8000, days_past_due=95)
        Payment.all_objects.filter(pk=paid(erased, 8000).pk).update(deleted_at=timezone.now())
        InvoiceFactory(organization=org, total_cents=99999)
        services.void_invoice(owed(org, total_cents=77777), reason="Wrong customer")
        return [untouched, part_paid, bounced, settled, overpaid, erased]

    def test_the_total_is_the_sum_of_every_invoices_balance(
        self, org, owner_client, awkward_ledger
    ):
        expected = sum(
            services.balance_cents(invoice) for invoice in Invoice.objects.filter(organization=org)
        )

        assert expected == 12345 + 10000 + 9000 + 8000
        assert receivables(owner_client)["totals"]["total_cents"] == expected

    def test_the_carried_figure_is_the_one_the_ledger_gives(self, org, awkward_ledger):
        """`with_paid_cents` and a per-invoice read, side by side."""
        carried = {
            invoice.pk: invoice
            for invoice in services.with_paid_cents(Invoice.objects.filter(organization=org))
        }

        for invoice in Invoice.objects.filter(organization=org):
            assert not hasattr(invoice, services.PAID_ANNOTATION)
            twin = carried[invoice.pk]
            assert services.paid_cents(twin) == services.paid_cents(invoice)
            assert services.balance_cents(twin) == services.balance_cents(invoice)
            assert services.payment_state(twin) == services.payment_state(invoice)

    def test_reading_the_carried_figure_costs_no_query(self, org, awkward_ledger):
        invoices = list(services.with_paid_cents(Invoice.objects.filter(organization=org)))

        with CaptureQueriesContext(connection) as captured:
            for invoice in invoices:
                services.balance_cents(invoice)

        assert len(captured) == 0


# --- receivables: aging ----------------------------------------------------


@pytest.mark.django_db
class TestReceivablesAging:
    @pytest.mark.parametrize(
        ("days_past_due", "bucket"),
        [
            (-10, "not_due_cents"),
            # Due today is not late yet: `services.is_overdue` says the same.
            (0, "not_due_cents"),
            (1, "days_1_30_cents"),
            (30, "days_1_30_cents"),
            (31, "days_31_60_cents"),
            (60, "days_31_60_cents"),
            (61, "days_61_90_cents"),
            (90, "days_61_90_cents"),
            (91, "days_over_90_cents"),
            (400, "days_over_90_cents"),
        ],
    )
    def test_each_edge_of_each_bucket(self, days_past_due, bucket, org, owner_client):
        owed(org, total_cents=15000, days_past_due=days_past_due)

        totals = receivables(owner_client)["totals"]

        assert totals[bucket] == 15000
        assert sum(totals[field] for field in reports.AGING_FIELDS) == totals["total_cents"]

    def test_not_yet_due_and_overdue_agree_with_the_invoice_page(self, org):
        """The bucket edge and `is_overdue` are the same rule, held together."""
        for days in (-1, 0, 1):
            invoice = owed(org, days_past_due=days)
            in_a_late_bucket = reports.aging_bucket(invoice.due_on, AS_OF) != "not_due_cents"

            assert in_a_late_bucket == services.is_overdue(invoice, today=AS_OF)

    def test_as_of_defaults_to_the_organizations_own_today(self, org, owner_client, monkeypatch):
        """Not UTC's date, which is tomorrow's for a Denver tenant every evening."""
        monkeypatch.setattr(Organization, "today", lambda self: dt.date(2027, 7, 1))
        owed(org, total_cents=15000, days_past_due=0)  # due 30 June

        report = owner_client.get(RECEIVABLES).json()

        assert report["as_of"] == "2027-07-01"
        assert report["totals"]["days_1_30_cents"] == 15000

    def test_as_of_moves_the_clock(self, org, owner_client):
        owed(org, total_cents=15000, days_past_due=0)  # due 30 June

        assert receivables(owner_client, as_of="2027-06-30")["totals"]["not_due_cents"] == 15000
        assert receivables(owner_client, as_of="2027-08-15")["totals"]["days_31_60_cents"] == 15000

    def test_as_of_does_not_rewind_the_ledger(self, org, owner_client):
        """
        Balances are what is owed now. A payment received after `as_of`
        still counts, because the question is "who owes me", not "who did".
        """
        paid(owed(org, total_cents=15000), 15000, received_on=dt.date(2027, 6, 29))

        assert receivables(owner_client, as_of="2027-06-01")["customers"] == []

    def test_a_bad_as_of_is_a_400(self, owner_client):
        for url in (RECEIVABLES, RECEIVABLES_CSV):
            response = owner_client.get(url, {"as_of": "30/06/2027"})

            assert response.status_code == 400
            assert "as_of" in response.json()


# --- receivables: soft deletes ---------------------------------------------


@pytest.mark.django_db
class TestReceivablesSoftDeletes:
    """
    The manager filters `Invoice` and nothing it joins to (CLAUDE.md gate
    item 5). Each relation is a decision, and each follows the foreign key
    and ADR-026: an invoice is a snapshot that outlives what it was built
    from.
    """

    def test_a_removed_customer_still_owes(self, org, owner_client):
        customer = CustomerFactory(organization=org, first_name="Gone", last_name="Away")
        owed(org, total_cents=15000, customer=customer)
        customer.delete()

        (row,) = receivables(owner_client)["customers"]

        assert row["customer_name"] == "Gone Away"
        assert row["total_cents"] == 15000

    def test_a_deleted_invoice_is_gone(self, org, owner_client):
        """Only a draft can be deleted through the model; the admin is not so bound."""
        invoice = owed(org, total_cents=15000)
        Invoice.all_objects.filter(pk=invoice.pk).update(deleted_at=timezone.now())

        assert receivables(owner_client)["customers"] == []

    def test_a_deleted_line_does_not_change_what_was_issued(self, org, owner_client):
        """The total is the snapshot on the invoice, never re-added from lines."""
        invoice = owed(org, total_cents=15000)
        line = InvoiceLineFactory(organization=org, invoice=invoice, amount_cents=15000)
        line.delete()

        assert receivables(owner_client)["totals"]["total_cents"] == 15000

    def test_a_deleted_job_does_not_unbill_its_invoice(self, org, owner_client):
        invoice = owed(org, total_cents=15000)
        line = InvoiceLineFactory(organization=org, invoice=invoice, amount_cents=15000)
        line.job.delete()

        assert receivables(owner_client)["totals"]["total_cents"] == 15000


# --- receivables: shape ----------------------------------------------------


@pytest.mark.django_db
class TestReceivablesShape:
    def test_customers_by_name_then_id(self, org, owner_client):
        zed = CustomerFactory(organization=org, first_name="zed", last_name="Young")
        amy = CustomerFactory(organization=org, first_name="Amy", last_name="Adams")
        twins = sorted(
            [
                CustomerFactory(organization=org, first_name="Sam", last_name="Same"),
                CustomerFactory(organization=org, first_name="Sam", last_name="Same"),
            ],
            key=lambda customer: str(customer.pk),
        )
        for customer in (zed, *twins, amy):
            owed(org, customer=customer)

        rows = receivables(owner_client)["customers"]

        assert [row["customer"] for row in rows] == [
            str(amy.pk),
            str(twins[0].pk),
            str(twins[1].pk),
            str(zed.pk),
        ]

    @pytest.mark.parametrize("url", [RECEIVABLES, RECEIVABLES_CSV])
    def test_the_same_data_gives_the_same_bytes(self, url, org, owner_client):
        for _ in range(4):
            customer = CustomerFactory(organization=org, first_name="Sam", last_name="Same")
            for days in (0, 20, 50):
                paid(owed(org, days_past_due=days, customer=customer), 1000)

        first = owner_client.get(url, {"as_of": "2027-06-30"})
        second = owner_client.get(url, {"as_of": "2027-06-30"})

        assert first.status_code == 200
        assert first.content == second.content

    @pytest.mark.parametrize("url", [RECEIVABLES, RECEIVABLES_CSV])
    def test_the_query_count_does_not_grow_with_customers(self, url, org, owner_client):
        def grow(customers):
            for _ in range(customers):
                customer = CustomerFactory(organization=org)
                for days in (0, 40):
                    paid(owed(org, days_past_due=days, customer=customer), 2500)

        def queries():
            with CaptureQueriesContext(connection) as captured:
                assert owner_client.get(url, {"as_of": "2027-06-30"}).status_code == 200
            return len(captured)

        grow(2)
        small = queries()
        grow(8)

        assert queries() == small


@pytest.mark.django_db
class TestReceivablesCSV:
    def test_it_is_a_download_named_for_its_date(self, owner_client):
        response = owner_client.get(RECEIVABLES_CSV, {"as_of": "2027-06-30"})

        assert response["Content-Type"] == "text/csv; charset=utf-8"
        assert response["Content-Disposition"] == (
            'attachment; filename="receivables_2027-06-30.csv"'
        )

    def test_asking_for_csv_by_name_is_not_refused(self, owner_client):
        response = owner_client.get(RECEIVABLES_CSV, headers={"Accept": "text/csv"})

        assert response.status_code == 200

    def test_amounts_are_decimal_dollars_and_the_header_says_so(self, org, owner_client):
        customer = CustomerFactory(organization=org, first_name="Dana", last_name="Hale")
        owed(org, total_cents=15007, days_past_due=0, customer=customer)
        owed(org, total_cents=5, days_past_due=45, customer=customer)

        rows = read_csv(owner_client.get(RECEIVABLES_CSV, {"as_of": "2027-06-30"}))

        assert rows == [
            [
                "customer",
                "invoices",
                "not_due_usd",
                "days_1_30_usd",
                "days_31_60_usd",
                "days_61_90_usd",
                "days_over_90_usd",
                "total_usd",
            ],
            ["Dana Hale", "2", "150.07", "0.00", "0.05", "0.00", "0.00", "150.12"],
        ]

    def test_the_csv_and_the_json_agree(self, org, owner_client):
        for days in (0, 10, 40, 70, 100):
            paid(owed(org, total_cents=12345, days_past_due=days), 345)

        report = receivables(owner_client)
        rows = read_csv(owner_client.get(RECEIVABLES_CSV, {"as_of": "2027-06-30"}))[1:]

        in_cents = sum(int(Decimal(row[-1]) * 100) for row in rows)
        assert in_cents == report["totals"]["total_cents"] == 5 * 12000
        assert len(rows) == len(report["customers"])

    @pytest.mark.parametrize(
        "name", ['=HYPERLINK("http://x","y")', "+1+1", "-10", "@SUM(A1:A9)", "\t=1+1", "\r=1+1"]
    )
    def test_a_customer_named_like_a_formula_is_written_as_text(self, name, org, owner_client):
        customer = CustomerFactory(organization=org, first_name="", last_name="", company_name=name)
        owed(org, total_cents=15000, customer=customer)

        row = read_csv(owner_client.get(RECEIVABLES_CSV, {"as_of": "2027-06-30"}))[1]

        assert row[0] == f"'{name}"
        # And the money beside it is still a number.
        assert row[-1] == "150.00"


# --- collections -----------------------------------------------------------


@pytest.mark.django_db
class TestCollections:
    def test_by_method_with_tips_fees_and_net(self, org, owner_client):
        paid(owed(org), 15000, method=PaymentMethod.CHECK)
        paid(owed(org), 10000, method=PaymentMethod.CHECK, tip_cents=2000)
        paid(owed(org), 4000, method=PaymentMethod.CASH, tip_cents=500)
        paid(
            owed(org),
            15000,
            method=PaymentMethod.CARD,
            provider="stripe",
            provider_reference="pi_1",
            fee_cents=465,
            recorded_by=None,
        )

        report = collections(owner_client)

        assert report["date_from"] == "2027-06-01"
        assert report["date_to"] == "2027-06-30"
        assert report["methods"] == [
            {
                "method": "cash",
                "label": "Cash",
                "payment_count": 1,
                "amount_cents": 4000,
                "tip_cents": 500,
                "fee_cents": 0,
                "net_cents": 4500,
                "fees_pending": 0,
            },
            {
                "method": "check",
                "label": "Check",
                "payment_count": 2,
                "amount_cents": 25000,
                "tip_cents": 2000,
                "fee_cents": 0,
                "net_cents": 27000,
                "fees_pending": 0,
            },
            {
                "method": "card",
                "label": "Card",
                "payment_count": 1,
                "amount_cents": 15000,
                "tip_cents": 0,
                "fee_cents": 465,
                "net_cents": 14535,
                "fees_pending": 0,
            },
        ]
        assert report["totals"] == {
            "payment_count": 4,
            "amount_cents": 44000,
            "tip_cents": 2500,
            "fee_cents": 465,
            "net_cents": 46035,
            "fees_pending": 0,
        }

    def test_methods_come_in_the_enums_order_and_only_if_used(self, org, owner_client):
        for method in (PaymentMethod.OTHER, PaymentMethod.ZELLE, PaymentMethod.CASH):
            paid(owed(org), 1000, method=method)

        report = collections(owner_client)

        assert [row["method"] for row in report["methods"]] == ["cash", "zelle", "other"]

    def test_nothing_received_is_zeros_not_an_error(self, owner_client):
        report = collections(owner_client)

        assert report["methods"] == []
        assert report["totals"]["net_cents"] == 0

    def test_a_payment_is_filed_under_received_on_at_both_ends(self, org, owner_client):
        for received_on in ("2027-05-31", "2027-06-01", "2027-06-30", "2027-07-01"):
            paid(owed(org), 1000, received_on=dt.date.fromisoformat(received_on))

        assert collections(owner_client)["totals"]["payment_count"] == 2

    def test_it_is_received_on_that_dates_it_not_when_it_was_typed_in(self, org, owner_client):
        """
        Every row here was created just now. Only the date on the check
        decides the period.
        """
        paid(owed(org), 7000, received_on=dt.date(2027, 6, 12))

        assert collections(owner_client)["totals"]["amount_cents"] == 7000
        this_month = timezone.now().date().replace(day=1)
        today = owner_client.get(
            COLLECTIONS,
            {"date_from": this_month.isoformat(), "date_to": str(timezone.now().date())},
        ).json()
        assert today["totals"]["payment_count"] == 0

    def test_a_voided_payment_is_in_no_period(self, org, owner_client):
        services.void_payment(paid(owed(org), 15000), reason="Bounced")
        paid(owed(org), 2000)

        report = collections(owner_client)

        assert report["totals"]["payment_count"] == 1
        assert report["totals"]["amount_cents"] == 2000

    def test_a_soft_deleted_payment_is_in_no_period(self, org, owner_client):
        payment = paid(owed(org), 15000)
        Payment.all_objects.filter(pk=payment.pk).update(deleted_at=timezone.now())

        assert collections(owner_client)["totals"]["payment_count"] == 0

    def test_money_against_a_since_voided_invoice_still_arrived(self, org, owner_client):
        """
        A card can be charged against an invoice voided in the meantime
        (`record_provider_payment`). It is in the bank until it is refunded,
        and the refund is what voids the row.
        """
        invoice = owed(org)
        Invoice.objects.filter(pk=invoice.pk).update(status=InvoiceStatus.VOID)
        paid(invoice, 15000, method=PaymentMethod.CARD, provider="stripe", recorded_by=None)

        assert collections(owner_client)["totals"]["amount_cents"] == 15000

    def test_a_disputed_payment_still_counts_until_it_is_lost(self, org, owner_client):
        paid(owed(org), 15000, disputed_at=timezone.now(), dispute_status="needs_response")

        assert collections(owner_client)["totals"]["amount_cents"] == 15000

    def test_a_card_fee_not_reported_yet_is_flagged_not_guessed(self, org, owner_client):
        card = {"method": PaymentMethod.CARD, "provider": "stripe", "recorded_by": None}
        paid(owed(org), 10000, provider_reference="pi_known", fee_cents=320, **card)
        paid(owed(org), 10000, provider_reference="pi_pending", fee_cents=None, **card)
        # A check never has a fee, so its empty fee is not "pending".
        paid(owed(org), 10000, method=PaymentMethod.CHECK)

        report = collections(owner_client)
        by_method = {row["method"]: row for row in report["methods"]}

        assert by_method["card"]["fee_cents"] == 320
        assert by_method["card"]["net_cents"] == 19680
        assert by_method["card"]["fees_pending"] == 1
        assert by_method["check"]["fees_pending"] == 0
        assert report["totals"]["fees_pending"] == 1

    @pytest.mark.parametrize("url", [COLLECTIONS, COLLECTIONS_CSV])
    @pytest.mark.parametrize(
        ("query", "field"),
        [
            ({}, "date_from"),
            ({"date_from": "2027-06-01"}, "date_to"),
            ({"date_from": "2027-06-30", "date_to": "2027-06-01"}, "date_to"),
            ({"date_from": "2026-01-01", "date_to": "2027-06-30"}, "date_to"),
        ],
    )
    def test_a_bad_period_is_a_400_naming_the_field(self, url, query, field, owner_client):
        response = owner_client.get(url, query)

        assert response.status_code == 400
        assert field in response.json()

    def test_a_whole_year_is_allowed(self, owner_client):
        response = owner_client.get(
            COLLECTIONS, {"date_from": "2027-01-01", "date_to": "2027-12-31"}
        )

        assert response.status_code == 200


@pytest.mark.django_db
class TestCollectionsShape:
    @pytest.mark.parametrize("url", [COLLECTIONS, COLLECTIONS_CSV])
    def test_the_same_data_gives_the_same_bytes(self, url, org, owner_client):
        for method in PaymentMethod.values:
            for _ in range(3):
                paid(owed(org), 1000, method=method, tip_cents=100)

        first = owner_client.get(url, JUNE)
        second = owner_client.get(url, JUNE)

        assert first.status_code == 200
        assert first.content == second.content

    @pytest.mark.parametrize("url", [COLLECTIONS, COLLECTIONS_CSV])
    def test_the_query_count_does_not_grow_with_payments_or_customers(self, url, org, owner_client):
        def grow(customers):
            for _ in range(customers):
                for method in (PaymentMethod.CASH, PaymentMethod.CHECK):
                    paid(owed(org), 1000, method=method)

        def queries():
            with CaptureQueriesContext(connection) as captured:
                assert owner_client.get(url, JUNE).status_code == 200
            return len(captured)

        grow(2)
        small = queries()
        grow(8)

        assert queries() == small


@pytest.mark.django_db
class TestCollectionsCSV:
    def test_it_is_a_download_named_for_its_period(self, owner_client):
        response = owner_client.get(COLLECTIONS_CSV, JUNE)

        assert response["Content-Type"] == "text/csv; charset=utf-8"
        assert response["Content-Disposition"] == (
            'attachment; filename="collections_2027-06-01_2027-06-30.csv"'
        )

    def test_asking_for_csv_by_name_is_not_refused(self, owner_client):
        response = owner_client.get(COLLECTIONS_CSV, JUNE, headers={"Accept": "text/csv"})

        assert response.status_code == 200

    def test_amounts_are_decimal_dollars_and_the_header_says_so(self, org, owner_client):
        paid(owed(org), 10007, method=PaymentMethod.MONEY_ORDER, tip_cents=5)
        paid(
            owed(org),
            15000,
            method=PaymentMethod.CARD,
            provider="stripe",
            provider_reference="pi_csv",
            fee_cents=465,
            recorded_by=None,
        )

        rows = read_csv(owner_client.get(COLLECTIONS_CSV, JUNE))

        assert rows == [
            ["method", "payments", "amount_usd", "tips_usd", "fees_usd", "net_usd", "fees_pending"],
            ["Money order", "1", "100.07", "0.05", "0.00", "100.12", "0"],
            ["Card", "1", "150.00", "0.00", "4.65", "145.35", "0"],
        ]
