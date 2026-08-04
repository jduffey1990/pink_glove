from rest_framework import serializers

from base.viewsets import TenantModelSerializer
from customers.models import Customer, ServiceLocation


class ServiceLocationSerializer(TenantModelSerializer):
    one_line_address = serializers.CharField(read_only=True)

    class Meta(TenantModelSerializer.Meta):
        model = ServiceLocation
        fields = (
            "id",
            "organization",
            "customer",
            "label",
            "line1",
            "line2",
            "city",
            "state",
            "postal_code",
            "country",
            "one_line_address",
            "square_feet",
            "bedrooms",
            "bathrooms",
            "parking_notes",
            "access_notes",
            "gate_code",
            "alarm_code",
            "key_location",
            "has_pets",
            "pet_notes",
            "is_active",
            "created_at",
            "updated_at",
        )


class ServiceLocationSummarySerializer(TenantModelSerializer):
    """
    Nested inside a customer. Deliberately omits the encrypted access fields:
    a list of customers should not spray gate codes across the wire.
    """

    one_line_address = serializers.CharField(read_only=True)

    class Meta(TenantModelSerializer.Meta):
        model = ServiceLocation
        fields = ("id", "label", "one_line_address", "square_feet", "is_active")
        read_only_fields = fields


class CustomerSerializer(TenantModelSerializer):
    display_name = serializers.CharField(read_only=True)
    locations = ServiceLocationSummarySerializer(many=True, read_only=True)

    class Meta(TenantModelSerializer.Meta):
        model = Customer
        fields = (
            "id",
            "organization",
            "user",
            "first_name",
            "last_name",
            "company_name",
            "display_name",
            "email",
            "phone",
            "preferred_contact_method",
            "billing_line1",
            "billing_line2",
            "billing_city",
            "billing_state",
            "billing_postal_code",
            "billing_country",
            "status",
            "source",
            "notes",
            "locations",
            "created_at",
            "updated_at",
        )

    def validate(self, attrs):
        # Mirrors the customer_has_a_name check constraint, so the API returns
        # a 400 with a useful message rather than a 500 from the database.
        merged = {**getattr(self.instance, "__dict__", {}), **attrs}
        if not any(
            str(merged.get(field) or "").strip()
            for field in ("first_name", "last_name", "company_name")
        ):
            raise serializers.ValidationError(
                "A customer needs at least a first name, last name, or company name."
            )
        return attrs
