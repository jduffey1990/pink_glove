from rest_framework import serializers

from base.viewsets import TenantModelSerializer
from customers.models import Customer, ServiceLocation
from users.enums import Role
from users.models import CustomUser


class ServiceLocationSerializer(TenantModelSerializer):
    """
    Access codes are write-only here.

    They can be set and changed through this endpoint, but reading them back
    requires the audited reveal action -- otherwise anyone opening a location
    detail page would see them with no record, which defeats the log.
    """

    one_line_address = serializers.CharField(read_only=True)

    gate_code = serializers.CharField(
        write_only=True, required=False, allow_blank=True, style={"input_type": "password"}
    )
    alarm_code = serializers.CharField(
        write_only=True, required=False, allow_blank=True, style={"input_type": "password"}
    )
    key_location = serializers.CharField(write_only=True, required=False, allow_blank=True)

    #: Lets the UI show "codes on file" without revealing them.
    has_access_codes = serializers.SerializerMethodField()

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
            "has_access_codes",
            "has_pets",
            "pet_notes",
            "is_active",
            "created_at",
            "updated_at",
        )

    def get_has_access_codes(self, obj) -> bool:
        return bool(obj.gate_code or obj.alarm_code or obj.key_location)


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

    def get_fields(self):
        """
        `user` may only be a customer-role member of the caller's organization.

        `CustomUser` is not a `TenantModel`, so the cross-tenant foreign key
        check in `TenantModel.save()` never looks at this field, and the default
        queryset is every account there is. Left open, a dispatcher could bind
        another organization's user -- or a superuser -- to their customer:
        the link is one-to-one, so that blocks the rightful organization from
        ever making it, and it is what decides whose jobs the portal shows.
        Narrowing the queryset rather than validating afterwards means a
        foreign id and a made-up one get the same "does not exist".
        """
        fields = super().get_fields()
        request = self.context.get("request")
        organization = getattr(request, "organization", None)

        fields["user"].queryset = CustomUser.objects.filter(
            memberships__organization=organization,
            memberships__role=Role.CUSTOMER,
            memberships__is_active=True,
            memberships__deleted_at__isnull=True,
        ).distinct()
        return fields

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
