from rest_framework import serializers

from organizations.models import Organization, validate_timezone


class OrganizationSerializer(serializers.ModelSerializer):
    timezone = serializers.CharField(validators=[validate_timezone])

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
            "is_active",
            "created_at",
        )
        read_only_fields = ("id", "slug", "is_active", "created_at")
