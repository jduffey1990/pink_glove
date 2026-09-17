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
