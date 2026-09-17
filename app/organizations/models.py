import datetime as dt
from zoneinfo import ZoneInfo, available_timezones

from django.core.exceptions import ValidationError
from django.db import models
from django.utils.text import slugify

from base.models import Base
from base.validators import MaxFileSize

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

    # Parked for Phase 4 -- tenants paying for the software, as distinct from
    # the tenant's own customers paying invoices. See ADR-007.
    stripe_customer_id = models.CharField(max_length=255, blank=True, default="", db_index=True)
    stripe_subscription_id = models.CharField(max_length=255, blank=True, default="")

    class Meta(Base.Meta):
        verbose_name = "Organization"
        verbose_name_plural = "Organizations"
        ordering = ("name",)

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

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
