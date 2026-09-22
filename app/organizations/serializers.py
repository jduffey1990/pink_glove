from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from organizations.models import Organization, validate_timezone, validate_working_days


class StripeStatusSerializer(serializers.Serializer):
    """
    The organization's Stripe Connect state, as one thing to look at.

    Read-only by construction: the fields it reads are written only by
    `billing.connect` and the webhook. `connected` is "there is an account";
    `charges_enabled` is "Stripe will take a card" -- the pay link waits for
    the second.
    """

    connected = serializers.BooleanField(read_only=True)
    charges_enabled = serializers.BooleanField(read_only=True)
    details_submitted = serializers.BooleanField(read_only=True)
    connected_at = serializers.DateTimeField(read_only=True, allow_null=True)


class OrganizationSerializer(serializers.ModelSerializer):
    """
    The organization as its own admins see it.

    Writes are gated to admin+ by `CurrentOrganizationView.get_permissions`,
    so the operating-window fields need no further permission handling here.
    """

    stripe = serializers.SerializerMethodField()

    timezone = serializers.CharField(validators=[validate_timezone])
    working_days = serializers.ListField(
        child=serializers.IntegerField(min_value=1, max_value=7),
        validators=[validate_working_days],
        help_text="ISO weekday numbers: 1 = Monday .. 7 = Sunday.",
        required=False,
    )

    class Meta:
        model = Organization
        fields = (
            "id",
            "name",
            "slug",
            "timezone",
            "email",
            "phone",
            "address_line1",
            "address_line2",
            "address_city",
            "address_state",
            "address_postal_code",
            "address_country",
            "logo",
            "primary_color",
            "business_hours_start",
            "business_hours_end",
            "working_days",
            "reveal_buffer_before_minutes",
            "reveal_buffer_after_minutes",
            "tax_rate_percent",
            "no_access_fee_type",
            "no_access_fee_value",
            "invoice_prefix",
            "invoice_terms_days",
            "invoice_footer",
            "stripe",
            "is_active",
            "created_at",
        )
        read_only_fields = ("id", "slug", "is_active", "created_at")

    @extend_schema_field(StripeStatusSerializer)
    def get_stripe(self, organization: Organization) -> dict:
        return {
            "connected": organization.stripe_connected,
            "charges_enabled": organization.stripe_charges_enabled,
            "details_submitted": organization.stripe_details_submitted,
            "connected_at": organization.stripe_connected_at,
        }
