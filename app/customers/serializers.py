from rest_framework import serializers

from base.validators import MaxFileSize
from base.viewsets import TenantModelSerializer
from customers.enums import ContactMethod, ImportAction
from customers.import_columns import MAX_UPLOAD_MEGABYTES
from customers.models import Customer, ServiceLocation
from users.enums import Role
from users.models import CustomUser

SMS_UNAVAILABLE = "Text messages aren't available yet."


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


class CustomerSummarySerializer(serializers.ModelSerializer):
    """
    A customer as another app's payload shows them: enough to know who this
    is and how to reach them, and nothing else.

    Lives here rather than in whichever app first needed it, so that changing
    what a customer summary contains is a change to `customers` -- scheduling
    and billing both read it, and neither owns it.
    """

    display_name = serializers.CharField(read_only=True)

    class Meta:
        model = Customer
        fields = ("id", "display_name", "phone", "preferred_contact_method")
        read_only_fields = fields


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

    #: Choices the enum keeps but a write refuses (ADR-035). The import's
    #: template hint reads this too, so it never offers what a row cannot hold.
    unavailable_choices = {"preferred_contact_method": frozenset({ContactMethod.SMS})}

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

    def validate_preferred_contact_method(self, value):
        """
        Nothing sends a text message yet, so nobody may be promised one
        (ADR-035). The enum keeps `sms` so rows that already hold it still
        read, and it comes back when SMS ships. Every write path reaches
        this: create, update, and the import, which validates rows here.
        """
        if value in self.unavailable_choices["preferred_contact_method"]:
            raise serializers.ValidationError(SMS_UNAVAILABLE)
        return value

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


# --- import (ADR-031) --------------------------------------------------------


class ImportedLocationSerializer(ServiceLocationSerializer):
    """
    `ServiceLocationSerializer` for a row of an import file.

    Every rule is inherited: this is the create path's validation, not a
    second copy of it. The one difference is who supplies `customer`. A row
    names its customer by email or name rather than by id, and in a dry run
    that customer does not exist yet, so the importer resolves it and passes
    it to `save()`, exactly as the viewset passes `organization`.
    """

    class Meta(ServiceLocationSerializer.Meta):
        read_only_fields = (*ServiceLocationSerializer.Meta.read_only_fields, "customer")


class CustomerImportUploadSerializer(serializers.Serializer):
    file = serializers.FileField(
        validators=[MaxFileSize(megabytes=MAX_UPLOAD_MEGABYTES)],
        help_text="A UTF-8 CSV file with a header row.",
    )
    # `default=True` is load-bearing, not decoration. A boolean missing from a
    # multipart form is an unticked checkbox to DRF, which is False unless the
    # field has a default, and False here means "write to the database". With
    # the default, a client that forgets the field gets a dry run.
    dry_run = serializers.BooleanField(
        default=True,
        help_text="True (the default) previews the import and writes nothing.",
    )


class CustomerImportRowErrorSerializer(serializers.Serializer):
    column = serializers.CharField(
        allow_blank=True, help_text="The file's header for the cell; blank for the whole row."
    )
    message = serializers.CharField()


class CustomerImportRowSerializer(serializers.Serializer):
    """One row's outcome. Never carries an access code (ADR-014)."""

    row = serializers.IntegerField(
        help_text="As a spreadsheet numbers it: the header is row 1, the first customer row 2."
    )
    action = serializers.ChoiceField(choices=ImportAction.choices)
    customer = serializers.CharField(allow_blank=True)
    address = serializers.CharField(allow_blank=True)
    detail = serializers.CharField()
    errors = CustomerImportRowErrorSerializer(many=True)


class CustomerImportTotalsSerializer(serializers.Serializer):
    rows = serializers.IntegerField()
    new_customers = serializers.IntegerField()
    new_locations = serializers.IntegerField()
    skipped = serializers.IntegerField()
    errors = serializers.IntegerField()


class CustomerImportResultSerializer(serializers.Serializer):
    dry_run = serializers.BooleanField()
    committed = serializers.BooleanField(
        help_text="False for a dry run, and for an import refused over a row."
    )
    can_commit = serializers.BooleanField(
        help_text="Whether sending this file again with dry_run false would write something."
    )
    message = serializers.CharField()
    totals = CustomerImportTotalsSerializer()
    ignored_columns = serializers.ListField(child=serializers.CharField())
    rows = CustomerImportRowSerializer(many=True)


class CustomerImportTemplateColumnSerializer(serializers.Serializer):
    header = serializers.CharField()
    required = serializers.BooleanField()
    hint = serializers.CharField(allow_blank=True)


class CustomerImportTemplateSerializer(serializers.Serializer):
    filename = serializers.CharField()
    csv = serializers.CharField(help_text="The header row and one example row.")
    columns = CustomerImportTemplateColumnSerializer(many=True)
    max_rows = serializers.IntegerField()
    max_megabytes = serializers.IntegerField()
