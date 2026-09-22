import datetime as dt
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo, available_timezones

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.text import slugify

from base.models import Base
from base.validators import MaxFileSize
from organizations.enums import NoAccessFeeType, StripeState

#: ISO weekday numbers, the convention `datetime.isoweekday()` returns.
MONDAY, SUNDAY = 1, 7


#: Monday through Friday. A callable, because a mutable default on a
#: JSONField is shared across every instance that does not override it.
def default_working_days() -> list[int]:
    return [1, 2, 3, 4, 5]


def validate_timezone(value: str) -> None:
    if value not in available_timezones():
        raise ValidationError(f"{value!r} is not a valid IANA timezone name.")


def validate_working_days(value) -> None:
    """A list of distinct ISO weekday numbers, 1 (Monday) through 7 (Sunday)."""
    if not isinstance(value, list):
        raise ValidationError("Working days must be a list of ISO weekday numbers.")

    for day in value:
        # bool is an int subclass, and `True` would silently mean Monday.
        if isinstance(day, bool) or not isinstance(day, int) or not MONDAY <= day <= SUNDAY:
            raise ValidationError(
                f"{day!r} is not an ISO weekday number (1 = Monday .. 7 = Sunday)."
            )

    if len(set(value)) != len(value):
        raise ValidationError("Working days must not repeat.")


class Organization(Base):
    """
    The tenant. One cleaning business.

    Everything tenant-owned points here through `base.models.TenantModel`.
    """

    name = models.CharField(max_length=255, unique=True)
    slug = models.SlugField(max_length=255, unique=True, blank=True)

    #: Load-bearing, not cosmetic. Recurring visits are authored in local time
    #: ("every Tuesday at 9am") and must survive DST transitions, so recurrence
    #: expands in this zone before being stored as UTC. See docs/DECISIONS.md
    #: ADR-012.
    timezone = models.CharField(
        max_length=64,
        default="America/Denver",
        validators=[validate_timezone],
        help_text="IANA timezone name, e.g. America/Denver.",
    )

    email = models.EmailField(blank=True, default="")
    phone = models.CharField(max_length=30, blank=True, default="")

    address_line1 = models.CharField(max_length=255, blank=True, default="")
    address_line2 = models.CharField(max_length=255, blank=True, default="")
    address_city = models.CharField(max_length=255, blank=True, default="")
    address_state = models.CharField(max_length=100, blank=True, default="")
    address_postal_code = models.CharField(max_length=30, blank=True, default="")
    address_country = models.CharField(max_length=2, blank=True, default="US")

    logo = models.ImageField(
        upload_to="organization_logos/", null=True, blank=True, validators=[MaxFileSize(2)]
    )
    primary_color = models.CharField(max_length=7, blank=True, default="")

    # --- Operating window ---------------------------------------------------
    # Read by the access-reveal evaluator (a reveal at 3am is worth a question)
    # and by the scheduling UI. Local wall-clock times in `timezone`, never UTC.

    business_hours_start = models.TimeField(default=dt.time(7, 0))
    business_hours_end = models.TimeField(default=dt.time(19, 0))
    working_days = models.JSONField(
        default=default_working_days,
        blank=True,
        validators=[validate_working_days],
        help_text="ISO weekday numbers: 1 = Monday .. 7 = Sunday.",
    )

    #: How far either side of a job's window a code reveal is unremarkable.
    #: Wider after than before on purpose -- a cleaner arriving early is
    #: unusual, one still finishing up late is not.
    reveal_buffer_before_minutes = models.PositiveIntegerField(default=60)
    reveal_buffer_after_minutes = models.PositiveIntegerField(default=120)

    is_active = models.BooleanField(default=True)

    # --- Billing settings ---------------------------------------------------
    # Read by `billing.services` when an invoice is drafted and frozen onto it
    # when it is issued (ADR-026). Changing one of these must never alter an
    # invoice that has already gone out, which is why the invoice keeps its own
    # copy rather than a pointer back here.

    #: One flat rate for the whole organization; each service says whether it
    #: is taxable. A rate per service location was rejected -- see ADR-026.
    #: Decimal, not cents: 8.25% is a rate, and rounding the rate rather than
    #: the total loses money (the same reasoning as `catalog.Service`).
    tax_rate_percent = models.DecimalField(
        max_digits=6,
        decimal_places=3,
        default=Decimal("0.000"),
        validators=[MinValueValidator(Decimal("0.000"))],
        help_text="Sales tax rate as a percentage, e.g. 8.250 for 8.25%.",
    )

    no_access_fee_type = models.CharField(
        max_length=16, choices=NoAccessFeeType.choices, default=NoAccessFeeType.NONE
    )
    no_access_fee_value = models.DecimalField(
        max_digits=10,
        decimal_places=3,
        default=Decimal("0.000"),
        validators=[MinValueValidator(Decimal("0.000"))],
        help_text="Cents when the fee is flat, a percentage when it is proportional.",
    )

    invoice_prefix = models.CharField(
        max_length=8, default="INV", help_text="Prefix on invoice numbers, e.g. INV-0001."
    )
    invoice_terms_days = models.PositiveSmallIntegerField(
        default=14, help_text="Days from issue to the due date."
    )
    invoice_footer = models.TextField(
        blank=True,
        default="",
        help_text='Printed at the foot of every invoice, e.g. "Make checks payable to ...".',
    )

    # Parked for Phase 4c -- tenants paying for the software, as distinct from
    # the tenant's own customers paying invoices. See ADR-024.
    stripe_customer_id = models.CharField(max_length=255, blank=True, default="", db_index=True)
    stripe_subscription_id = models.CharField(max_length=255, blank=True, default="")

    # --- Stripe Connect (Phase 4b, ADR-024) ---------------------------------
    # The tenant's own Stripe account, on which its customers' card payments
    # are charged directly. Written only by `billing.connect` and the
    # `account.updated` webhook handler -- never from request data; the
    # serializer publishes them read-only as one `stripe` block.

    stripe_account_id = models.CharField(max_length=64, blank=True, default="")
    #: Mirrors `Account.charges_enabled`: Stripe has finished its checks and
    #: will accept a charge. The pay link appears only once this is true.
    stripe_charges_enabled = models.BooleanField(default=False)
    #: Mirrors `Account.details_submitted`: the owner finished the onboarding
    #: form. True with charges still off means Stripe is reviewing.
    stripe_details_submitted = models.BooleanField(default=False)
    #: When charges first became enabled.
    stripe_connected_at = models.DateTimeField(null=True, blank=True)

    class Meta(Base.Meta):
        verbose_name = "Organization"
        verbose_name_plural = "Organizations"
        ordering = ("name",)
        constraints = [
            # A webhook resolves its `account` to exactly one tenant. Two
            # organizations sharing a Stripe account would make every payment
            # ambiguous, so the database refuses it.
            models.UniqueConstraint(
                fields=["stripe_account_id"],
                condition=~models.Q(stripe_account_id="") & models.Q(deleted_at__isnull=True),
                name="one_organization_per_stripe_account",
            )
        ]

    @property
    def stripe_connected(self) -> bool:
        return bool(self.stripe_account_id)

    @property
    def stripe_state(self) -> str:
        if self.stripe_charges_enabled:
            return StripeState.ENABLED
        if self.stripe_account_id:
            return StripeState.PENDING
        return StripeState.NOT_CONNECTED

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def today(self) -> dt.date:
        """
        This organization's current local date.

        The one date the business reckons by: what the schedule calls today,
        and what an invoice is issued on. `timezone.now().date()` is UTC's
        answer, which is tomorrow's for a Denver tenant after 5pm.
        """
        return timezone.now().astimezone(self.tz).date()

    def local_day_bounds(
        self, date_from: dt.date | None = None, date_to: dt.date | None = None
    ) -> tuple[dt.datetime | None, dt.datetime | None]:
        """
        The UTC interval covering [date_from, date_to] as *this* organization
        reckons those days. Either end may be None, and comes back None.

        Half-open: `start <= x < end`, with `end` at the midnight following
        `date_to`. Cleaner than `time.max`, and it does not lose the last
        microsecond of the day. Callers filter with `__gte` and `__lt`.
        """
        start = end = None

        if date_from is not None:
            start = dt.datetime.combine(date_from, dt.time.min, tzinfo=self.tz)
        if date_to is not None:
            end = dt.datetime.combine(date_to + dt.timedelta(days=1), dt.time.min, tzinfo=self.tz)

        return start, end

    def no_access_fee_cents(self, job_price_cents: int) -> int:
        """
        What a no-access visit costs, in whole cents.

        NONE is zero and the caller skips the line entirely -- an organization
        that does not charge for a locked door should not see a $0.00 row on
        the invoice explaining that it did not.

        Rounded once, half up, like every other money calculation here.
        """
        if self.no_access_fee_type == NoAccessFeeType.FLAT:
            raw = Decimal(self.no_access_fee_value)
        elif self.no_access_fee_type == NoAccessFeeType.PERCENT:
            raw = Decimal(job_price_cents) * Decimal(self.no_access_fee_value) / Decimal(100)
        else:
            return 0

        return int(raw.quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    def is_within_business_hours(self, when: dt.datetime) -> bool:
        """
        Is `when` inside this organization's operating window?

        Converts to the organization's own timezone first: the caller almost
        always holds UTC, and "was this 9pm for them" is the only question
        worth asking. A naive datetime is taken as already local.

        The window is inclusive of both ends, and an end earlier than the start
        (an overnight crew) wraps past midnight.
        """
        local = when.astimezone(self.tz) if when.tzinfo is not None else when

        if local.isoweekday() not in self.working_days:
            return False

        start, end = self.business_hours_start, self.business_hours_end
        if start <= end:
            return start <= local.time() <= end
        # Wraps midnight: inside means after the start OR before the end.
        return local.time() >= start or local.time() <= end
