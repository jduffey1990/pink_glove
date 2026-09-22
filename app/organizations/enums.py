from django.db.models import TextChoices


class NoAccessFeeType(TextChoices):
    """
    What this organization charges when the crew arrived and could not get in.

    Kept as a type plus a value rather than two nullable amount fields: "flat
    $25" and "50% of the visit" are the two ways every cleaning company writes
    this rule, and NONE is a real answer that must not read as "$0 flat".
    """

    NONE = "none", "No charge"
    FLAT = "flat", "Flat amount"
    PERCENT = "percent", "Percentage of the visit price"


class StripeState(TextChoices):
    """
    Where the organization is with Stripe Connect (Phase 4b), as one word
    the settings page switches on rather than re-deriving from the flags
    (ADR-023).
    """

    NOT_CONNECTED = "not_connected", "Not connected"
    #: An account exists; Stripe has not yet enabled charges on it.
    PENDING = "pending", "Pending with Stripe"
    ENABLED = "enabled", "Taking card payments"
