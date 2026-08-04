from django.db.models import TextChoices


class PricingModel(TextChoices):
    FLAT = "flat", "Flat rate"
    HOURLY = "hourly", "Hourly"
    PER_SQFT = "per_sqft", "Per square foot"
