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


class BillingState(TextChoices):
    """
    Where the organization is with its pink_glove subscription (Phase 4c,
    ADR-028), as the one word the banner and the settings card switch on
    (ADR-023). Derived by `organizations.services.billing_state`, never
    stored.
    """

    #: Comped by hand, or platform billing is not configured on the server.
    EXEMPT = "exempt", "Complimentary"
    #: The application-side trial is running and there is no live subscription.
    TRIAL = "trial", "On trial"
    #: Stripe says active or trialing, and the organization is paid through a
    #: future date -- or paid through one on a subscription since cancelled.
    ACTIVE = "active", "Subscribed"
    #: A renewal failed and Stripe is retrying the card.
    PAST_DUE = "past_due", "Payment failed"
    #: Coverage has ended and the grace period is running.
    LAPSED = "lapsed", "Lapsed"
    #: `is_active` is false, for billing or by hand: read-only.
    DEACTIVATED = "deactivated", "Deactivated"


class NoticeLevel(TextChoices):
    """How loudly the banner says what `billing_notice` has to say."""

    INFO = "info", "Info"
    WARNING = "warning", "Warning"
    ERROR = "error", "Error"
