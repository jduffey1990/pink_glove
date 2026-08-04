from django.db.models import TextChoices


class CustomerStatus(TextChoices):
    LEAD = "lead", "Lead"
    ACTIVE = "active", "Active"
    INACTIVE = "inactive", "Inactive"
    ARCHIVED = "archived", "Archived"


class ContactMethod(TextChoices):
    EMAIL = "email", "Email"
    SMS = "sms", "Text message"
    PHONE = "phone", "Phone call"
