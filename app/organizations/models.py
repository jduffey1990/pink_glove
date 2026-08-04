from zoneinfo import available_timezones

from django.core.exceptions import ValidationError
from django.db import models
from django.utils.text import slugify

from base.models import Base


def validate_timezone(value: str) -> None:
    if value not in available_timezones():
        raise ValidationError(f"{value!r} is not a valid IANA timezone name.")


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

    logo = models.ImageField(upload_to="organization_logos/", null=True, blank=True)
    primary_color = models.CharField(max_length=7, blank=True, default="")

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
