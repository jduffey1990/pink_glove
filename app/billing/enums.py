from django.db.models import TextChoices


class InvoiceStatus(TextChoices):
    """
    Where an invoice is in its life, and nothing about whether it is paid.

    PAID, PARTIALLY_PAID and OVERDUE are deliberately absent. They are derived
    from the payment ledger and the due date (`billing.services.payment_state`,
    ADR-025); storing them would be a second source of truth that a voided
    payment silently falsifies.
    """

    DRAFT = "draft", "Draft"
    ISSUED = "issued", "Issued"
    VOID = "void", "Void"


class PaymentState(TextChoices):
    """
    Derived from the ledger, never stored. Published on the invoice so the
    pages do not each work it out from a list of payments.
    """

    UNPAID = "unpaid", "Unpaid"
    PARTIAL = "partial", "Partly paid"
    PAID = "paid", "Paid"


class PaymentMethod(TextChoices):
    """
    How the money arrived.

    Cash, check, money order and Zelle are not workarounds beside a card
    system -- for a cleaning company they are most of the money (ADR-025).
    CARD is written by a Stripe webhook from Phase 4b; nothing else about
    such a row differs.
    """

    CASH = "cash", "Cash"
    CHECK = "check", "Check"
    MONEY_ORDER = "money_order", "Money order"
    ZELLE = "zelle", "Zelle"
    CARD = "card", "Card"
    OTHER = "other", "Other"


class LineKind(TextChoices):
    VISIT = "visit", "Visit"
    NO_ACCESS_FEE = "no_access_fee", "No-access fee"
    ADJUSTMENT = "adjustment", "Adjustment"


class StripeEventStatus(TextChoices):
    """
    Where a webhook event is in its life.

    RECEIVED is written in the request, before the 200; a handler moves it
    on from a worker. FAILED events are the queue a person looks at.
    """

    RECEIVED = "received", "Received"
    PROCESSED = "processed", "Processed"
    #: No handler for the type, or no organization owns the account. Kept,
    #: because "we never got it" and "we ignored it" are different disputes.
    IGNORED = "ignored", "Ignored"
    FAILED = "failed", "Failed"
