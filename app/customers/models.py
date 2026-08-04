from django.db import models

from base.fields import EncryptedTextField
from base.models import TenantModel
from customers.enums import ContactMethod, CustomerStatus
from users.models import CustomUser


class Customer(TenantModel):
    """
    Someone the cleaning company cleans for.

    Distinct from `CustomUser`: most customers never sign in. `user` is
    populated only when they use the portal, which happens by magic link
    (docs/DECISIONS.md ADR-008).
    """

    user = models.OneToOneField(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="customer_profile",
        help_text="Set only if this customer has portal access.",
    )

    first_name = models.CharField(max_length=150, blank=True, default="")
    last_name = models.CharField(max_length=150, blank=True, default="")
    company_name = models.CharField(
        max_length=255, blank=True, default="", help_text="For commercial accounts."
    )

    email = models.EmailField(blank=True, default="", db_index=True)
    phone = models.CharField(max_length=30, blank=True, default="", db_index=True)
    preferred_contact_method = models.CharField(
        max_length=16, choices=ContactMethod.choices, default=ContactMethod.EMAIL
    )

    # Where invoices go. The address a job happens at lives on ServiceLocation
    # -- they differ often enough (landlords, property managers) to keep apart.
    billing_line1 = models.CharField(max_length=255, blank=True, default="")
    billing_line2 = models.CharField(max_length=255, blank=True, default="")
    billing_city = models.CharField(max_length=255, blank=True, default="")
    billing_state = models.CharField(max_length=100, blank=True, default="")
    billing_postal_code = models.CharField(max_length=30, blank=True, default="")
    billing_country = models.CharField(max_length=2, blank=True, default="US")

    status = models.CharField(
        max_length=16, choices=CustomerStatus.choices, default=CustomerStatus.LEAD, db_index=True
    )
    source = models.CharField(
        max_length=255, blank=True, default="", help_text="How they found you."
    )
    notes = models.TextField(blank=True, default="")

    class Meta(TenantModel.Meta):
        verbose_name = "Customer"
        verbose_name_plural = "Customers"
        ordering = ("last_name", "first_name", "company_name")
        constraints = [
            models.CheckConstraint(
                condition=models.Q(first_name__gt="")
                | models.Q(last_name__gt="")
                | models.Q(company_name__gt=""),
                name="customer_has_a_name",
            )
        ]

    def __str__(self):
        return self.display_name

    @property
    def display_name(self) -> str:
        person = f"{self.first_name} {self.last_name}".strip()
        if person and self.company_name:
            return f"{self.company_name} ({person})"
        return person or self.company_name or self.email or str(self.id)


class ServiceLocation(TenantModel):
    """
    A physical address that gets cleaned.

    One customer can have several: a residence, a rental, an office.
    Job pricing reads `square_feet` for per-sqft services.
    """

    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="locations")

    label = models.CharField(
        max_length=255, blank=True, default="", help_text='e.g. "Home", "Rental - Oak St".'
    )

    line1 = models.CharField(max_length=255)
    line2 = models.CharField(max_length=255, blank=True, default="")
    city = models.CharField(max_length=255)
    state = models.CharField(max_length=100)
    postal_code = models.CharField(max_length=30, db_index=True)
    country = models.CharField(max_length=2, default="US")

    square_feet = models.PositiveIntegerField(null=True, blank=True)
    bedrooms = models.PositiveSmallIntegerField(null=True, blank=True)
    bathrooms = models.DecimalField(
        max_digits=4, decimal_places=1, null=True, blank=True, help_text="Half baths as .5"
    )

    parking_notes = models.TextField(blank=True, default="")
    access_notes = models.TextField(
        blank=True, default="", help_text="Non-sensitive entry instructions."
    )

    # Encrypted: a leaked dump of these is a physical-security incident, not
    # just a privacy one. Cannot be searched or filtered on -- see base.fields.
    gate_code = EncryptedTextField(blank=True, default="")
    alarm_code = EncryptedTextField(blank=True, default="")
    key_location = EncryptedTextField(blank=True, default="")

    has_pets = models.BooleanField(default=False)
    pet_notes = models.TextField(blank=True, default="")

    is_active = models.BooleanField(default=True, db_index=True)

    class Meta(TenantModel.Meta):
        verbose_name = "Service location"
        verbose_name_plural = "Service locations"
        ordering = ("customer", "label")

    def __str__(self):
        return f"{self.label or self.line1} ({self.customer.display_name})"

    @property
    def one_line_address(self) -> str:
        parts = [self.line1, self.line2, self.city, self.state, self.postal_code]
        return ", ".join(part for part in parts if part)
