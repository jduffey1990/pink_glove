"""
The money reports: who owes the organization, and what came in.

Read-only, and neither holds a formula of its own. A balance is
`services.balance_cents` and a live payment is `services.NOT_VOIDED`, the
same ones the invoice page and the pay link use, so a report cannot disagree
with the screen beside it (ADR-025). What is decided here is only how the
answers are grouped, and each grouping is recorded in ADR-030:

* **Receivables age by due date, against the live ledger.** `as_of` moves the
  clock the ages are counted from and nothing else: the balances are what is
  owed now. It is not a rewind to what was owed on that date.
* **Collections file a payment under `received_on`**, the organization-local
  date written on the row, not the instant the row was typed in. A check
  that arrived on Friday and was entered on Monday is Friday's money.
* **A voided payment is not in any period.** Voiding says the money did not
  arrive, or went back, so it leaves the month it was first counted in.

Soft deletes follow the foreign keys, as in `scheduling.reports`. An invoice
is protected from its customer going, so a debt outlives the address-book
entry and stays in receivables under the customer's name. Lines are not read
at all: an issued invoice's total is the snapshot on the row (ADR-026).
"""

import datetime as dt

from django.db import models
from django.db.models.functions import Coalesce

from base.exports import cents_as_dollars
from billing import services
from billing.enums import InvoiceStatus, PaymentMethod
from billing.models import Invoice, Payment

# ---------------------------------------------------------------------------
# Receivables
# ---------------------------------------------------------------------------

#: The aging buckets, youngest first, as (field, last day past due it holds).
#: None is open-ended. "Not yet due" includes the due date itself: an invoice
#: is overdue the day after, which is `services.is_overdue`'s rule too.
AGING_BUCKETS: tuple[tuple[str, int | None], ...] = (
    ("not_due_cents", 0),
    ("days_1_30_cents", 30),
    ("days_31_60_cents", 60),
    ("days_61_90_cents", 90),
    ("days_over_90_cents", None),
)

AGING_FIELDS = tuple(field for field, _ in AGING_BUCKETS)


def aging_bucket(due_on: dt.date | None, as_of: dt.date) -> str:
    """
    Which bucket an invoice due on `due_on` sits in on `as_of`.

    An issued invoice always has a due date; one without is treated as not
    yet due rather than guessed to be late, as `services.is_overdue` does.
    """
    days_past_due = 0 if due_on is None else (as_of - due_on).days

    for field, last_day in AGING_BUCKETS:
        if last_day is not None and days_past_due <= last_day:
            return field

    return AGING_FIELDS[-1]


def _empty_aging() -> dict:
    return {"invoice_count": 0, **dict.fromkeys(AGING_FIELDS, 0), "total_cents": 0}


def receivables(organization, *, as_of: dt.date | None = None) -> dict:
    """
    What each customer owes, aged by due date as of `as_of` (default: the
    organization's own today).

    Issued invoices only. A draft is not a debt, a void invoice is not one
    any more, and `services.balance_cents` already answers nothing for both.

    One query, whatever the number of customers or invoices. Customers are
    ordered by name and then id.
    """
    as_of = as_of or organization.today()

    # The status and the paid-less-than-total filters only narrow the fetch to
    # invoices that can have a balance, so a tenant's years of drafts, voids
    # and settled invoices are not read to be skipped. Neither decides
    # anything: the balance is `services.balance_cents`, below, and it would
    # answer nothing for every row they leave out.
    owing = (
        services.with_paid_cents(
            Invoice.objects.filter(organization=organization, status=InvoiceStatus.ISSUED)
        )
        .filter(**{f"{services.PAID_ANNOTATION}__lt": models.F("total_cents")})
        # A join, so a soft-deleted customer still comes back: on purpose.
        .select_related("customer")
        .order_by("pk")
    )

    customers: dict = {}
    for invoice in owing:
        balance = services.balance_cents(invoice)
        if balance <= 0:
            continue

        row = customers.setdefault(
            invoice.customer_id,
            {
                "customer": invoice.customer_id,
                "customer_name": invoice.customer.display_name,
                **_empty_aging(),
            },
        )
        row["invoice_count"] += 1
        row[aging_bucket(invoice.due_on, as_of)] += balance
        row["total_cents"] += balance

    rows = sorted(
        customers.values(), key=lambda r: (r["customer_name"].casefold(), str(r["customer"]))
    )

    totals = _empty_aging()
    for row in rows:
        for field in totals:
            totals[field] += row[field]

    return {"as_of": as_of, "customers": rows, "totals": totals}


RECEIVABLES_CSV_HEADER = (
    "customer",
    "invoices",
    "not_due_usd",
    "days_1_30_usd",
    "days_31_60_usd",
    "days_61_90_usd",
    "days_over_90_usd",
    "total_usd",
)


def receivables_csv_rows(report: dict) -> list[tuple]:
    """One row per customer. Amounts are decimal dollars, as the header says."""
    return [
        (
            row["customer_name"],
            row["invoice_count"],
            *(cents_as_dollars(row[field]) for field in AGING_FIELDS),
            cents_as_dollars(row["total_cents"]),
        )
        for row in report["customers"]
    ]


# ---------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------

#: The order methods are listed in: the enum's own, so the report reads the
#: same way the payment form does.
METHOD_ORDER = {method: index for index, method in enumerate(PaymentMethod.values)}


def _empty_collections() -> dict:
    return {
        "payment_count": 0,
        "amount_cents": 0,
        "tip_cents": 0,
        "fee_cents": 0,
        "net_cents": 0,
        "fees_pending": 0,
    }


def collections(organization, *, date_from: dt.date, date_to: dt.date) -> dict:
    """
    Money received in [date_from, date_to], by method.

    Both dates are organization-local and compared straight against
    `received_on`, which is one too: there is no instant to convert.

    Per method: what settled invoices (`amount_cents`), tips, which never do
    (ADR-025), the provider's fees where it has reported them, and the net,
    which is amount plus tips less fees -- what reached the organization. A
    card payment whose fee Stripe has not reported yet has no fee to subtract;
    `fees_pending` counts those, so a net that is still too high says so.

    A payment against an invoice that was voided afterwards still counts: it
    arrived, and a refund is what voids it. One query.
    """
    by_method = (
        Payment.objects.filter(
            services.NOT_VOIDED,
            organization=organization,
            received_on__gte=date_from,
            received_on__lte=date_to,
        )
        .values("method")
        .annotate(
            payment_count=models.Count("pk"),
            amount=Coalesce(models.Sum("amount_cents"), 0),
            tips=Coalesce(models.Sum("tip_cents"), 0),
            fees=Coalesce(models.Sum("fee_cents"), 0),
            fees_pending=models.Count(
                "pk", filter=models.Q(fee_cents__isnull=True) & ~models.Q(provider="")
            ),
        )
    )

    methods = [
        {
            "method": row["method"],
            "label": PaymentMethod(row["method"]).label,
            "payment_count": row["payment_count"],
            "amount_cents": row["amount"],
            "tip_cents": row["tips"],
            "fee_cents": row["fees"],
            "net_cents": row["amount"] + row["tips"] - row["fees"],
            "fees_pending": row["fees_pending"],
        }
        for row in sorted(by_method, key=lambda r: METHOD_ORDER[r["method"]])
    ]

    totals = _empty_collections()
    for row in methods:
        for field in totals:
            totals[field] += row[field]

    return {"date_from": date_from, "date_to": date_to, "methods": methods, "totals": totals}


COLLECTIONS_CSV_HEADER = (
    "method",
    "payments",
    "amount_usd",
    "tips_usd",
    "fees_usd",
    "net_usd",
    "fees_pending",
)


def collections_csv_rows(report: dict) -> list[tuple]:
    """One row per method. Amounts are decimal dollars, as the header says."""
    return [
        (
            row["label"],
            row["payment_count"],
            cents_as_dollars(row["amount_cents"]),
            cents_as_dollars(row["tip_cents"]),
            cents_as_dollars(row["fee_cents"]),
            cents_as_dollars(row["net_cents"]),
            row["fees_pending"],
        )
        for row in report["methods"]
    ]
