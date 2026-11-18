from django.db.models import TextChoices


class CustomerStatus(TextChoices):
    LEAD = "lead", "Lead"
    ACTIVE = "active", "Active"
    INACTIVE = "inactive", "Inactive"
    ARCHIVED = "archived", "Archived"


class ContactMethod(TextChoices):
    """
    SMS stays in the enum so existing rows read, but no API write may set it
    until text messages exist (`CustomerSerializer`, ADR-035).
    """

    EMAIL = "email", "Email"
    SMS = "sms", "Text message"
    PHONE = "phone", "Phone call"


class ImportAction(TextChoices):
    """What an import does, or would do, with one row of the file (ADR-031)."""

    CREATE_CUSTOMER = "create_customer", "New customer"
    ADD_LOCATION = "add_location", "New location"
    SKIP = "skip", "Already on file"
    ERROR = "error", "Needs fixing"
