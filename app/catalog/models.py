"""
The service catalog and its pricing.

Money convention (CLAUDE.md invariant 5): *amounts* are integer cents, always.
*Rates* are Decimal, because $0.125 per square foot is a real price and
rounding the rate rather than the total loses money over a large house. The
quote is rounded to whole cents exactly once, at the end.
"""

from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models

from base.models import TenantModel
from catalog.enums import PricingModel


class Service(TenantModel):
    """A line on the menu: standard clean, deep clean, move-out, and so on."""

    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")

    pricing_model = models.CharField(
        max_length=16, choices=PricingModel.choices, default=PricingModel.FLAT
    )

    #: Used when pricing_model is FLAT. Also acts as a floor for the other
    #: models -- a 400 sqft studio should not price below your callout cost.
    base_price_cents = models.PositiveIntegerField(
        default=0, help_text="Flat price, or the minimum charge for other models."
    )
    hourly_rate_cents = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Cents per hour. Used when pricing model is Hourly.",
    )
    per_sqft_rate_cents = models.DecimalField(
        max_digits=10,
        decimal_places=3,
        default=Decimal("0.000"),
        validators=[MinValueValidator(Decimal("0.000"))],
        help_text="Cents per square foot. Used when pricing model is Per square foot.",
    )

    default_duration_minutes = models.PositiveIntegerField(
        default=120, help_text="Used to lay out the schedule before actual times are known."
    )

    is_active = models.BooleanField(default=True, db_index=True)

    #: Snapshotted onto each invoice line at draft time (ADR-026). Defaults to
    #: false because cleaning labour is untaxed in most US jurisdictions, and a
    #: wrong `true` overcharges a customer where a wrong `false` does not.
    is_taxable = models.BooleanField(
        default=False, help_text="Whether sales tax applies to this service."
    )

    class Meta(TenantModel.Meta):
        verbose_name = "Service"
        verbose_name_plural = "Services"
        ordering = ("name",)
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "name"],
                condition=models.Q(deleted_at__isnull=True),
                name="unique_active_service_name_per_organization",
            )
        ]

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        required = {
            PricingModel.HOURLY: ("hourly_rate_cents", "an hourly rate"),
            PricingModel.PER_SQFT: ("per_sqft_rate_cents", "a per-square-foot rate"),
        }.get(self.pricing_model)

        if required and not getattr(self, required[0]):
            raise ValidationError(
                {required[0]: f"A {self.pricing_model} service needs {required[1]}."}
            )

        if self.pricing_model == PricingModel.FLAT and not self.base_price_cents:
            raise ValidationError({"base_price_cents": "A flat-rate service needs a price."})

    def quote_cents(
        self, *, square_feet: int | None = None, hours: Decimal | float | None = None
    ) -> int:
        """
        Price this service for a given location or duration.

        Returns whole cents. Never falls below `base_price_cents`, which is why
        that field doubles as a minimum charge.

        Raises ValueError rather than guessing when the input the pricing model
        needs is missing -- silently quoting zero for a job is worse than
        failing loudly.
        """
        if self.pricing_model == PricingModel.FLAT:
            return int(self.base_price_cents)

        if self.pricing_model == PricingModel.HOURLY:
            if hours is None:
                raise ValueError(f"{self.name} is priced hourly, so `hours` is required.")
            raw = self.hourly_rate_cents * Decimal(str(hours))

        elif self.pricing_model == PricingModel.PER_SQFT:
            if square_feet is None:
                raise ValueError(
                    f"{self.name} is priced per square foot, so `square_feet` is required."
                )
            raw = self.per_sqft_rate_cents * Decimal(square_feet)

        else:  # pragma: no cover - guarded by field choices
            raise ValueError(f"Unknown pricing model {self.pricing_model!r}.")

        # Round once, at the end, so per-unit rounding error cannot accumulate.
        rounded = int(raw.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        return max(rounded, int(self.base_price_cents))
