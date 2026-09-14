"""
Serializers for the schedule.

The job serializer nests read-only summaries of the location, customer and
service rather than returning bare ids. That is deliberate: a cleaner needs the
address, the gate instructions and the customer's phone number to do the visit,
but the customer and location endpoints themselves are dispatcher-only. Nesting
here means a cleaner's phone makes one request and never needs an endpoint they
are not allowed to reach.

What is never nested is the access codes. Those come only from the audited
reveal action (ADR-016).
"""

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from base.viewsets import TenantModelSerializer
from catalog.models import Service
from customers.models import Customer, ServiceLocation
from scheduling.enums import JobStatus
from scheduling.models import (
    Job,
    JobAssignment,
    JobNote,
    JobPhoto,
    RecurringPlan,
    TimeEntry,
)
from scheduling.models import validate_rrule as _validate_rrule


def validate_rrule_field(value: str) -> str:
    """Serializer-level RRULE check, sharing the model's rule so the two cannot drift."""
    message = _validate_rrule(value)
    if message:
        raise serializers.ValidationError(message)
    return value


class LocationSummarySerializer(serializers.ModelSerializer):
    """
    What a cleaner needs to get in the door and do the work -- and nothing more.

    `has_access_codes` says whether codes exist; the codes themselves come only
    from the audited reveal action, which writes a row saying who looked.
    """

    one_line_address = serializers.CharField(read_only=True)
    has_access_codes = serializers.SerializerMethodField()

    class Meta:
        model = ServiceLocation
        fields = (
            "id",
            "label",
            "one_line_address",
            "access_notes",
            "parking_notes",
            "has_pets",
            "pet_notes",
            "has_access_codes",
        )
        read_only_fields = fields

    def get_has_access_codes(self, obj) -> bool:
        return bool(obj.gate_code or obj.alarm_code or obj.key_location)


class CustomerSummarySerializer(serializers.ModelSerializer):
    display_name = serializers.CharField(read_only=True)

    class Meta:
        model = Customer
        fields = ("id", "display_name", "phone", "preferred_contact_method")
        read_only_fields = fields


class ServiceSummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = Service
        fields = ("id", "name", "default_duration_minutes")
        read_only_fields = fields


class JobAssignmentSerializer(TenantModelSerializer):
    user_name = serializers.CharField(source="user.full_name", read_only=True)
    user_email = serializers.EmailField(source="user.email", read_only=True)

    class Meta(TenantModelSerializer.Meta):
        model = JobAssignment
        fields = ("id", "user", "user_name", "user_email", "assigned_at", "accepted_at")
        read_only_fields = fields


class TimeEntrySerializer(TenantModelSerializer):
    user_name = serializers.CharField(source="user.full_name", read_only=True)
    duration_minutes = serializers.IntegerField(read_only=True, allow_null=True)

    class Meta(TenantModelSerializer.Meta):
        model = TimeEntry
        fields = (
            "id",
            "job",
            "user",
            "user_name",
            "clock_in",
            "clock_out",
            "duration_minutes",
            "created_at",
        )
        # Dispatchers correct times through PATCH; everything else is derived.
        read_only_fields = ("id", "job", "user", "user_name", "duration_minutes", "created_at")

    def validate(self, attrs):
        clock_in = attrs.get("clock_in", getattr(self.instance, "clock_in", None))
        clock_out = attrs.get("clock_out", getattr(self.instance, "clock_out", None))

        if clock_in and clock_out and clock_out <= clock_in:
            raise serializers.ValidationError(
                {"clock_out": "A time entry must end after it starts."}
            )
        return attrs


class JobNoteSerializer(TenantModelSerializer):
    user_name = serializers.CharField(source="user.full_name", read_only=True)

    class Meta(TenantModelSerializer.Meta):
        model = JobNote
        fields = ("id", "job", "user", "user_name", "body", "created_at")
        # `user` is stamped from the request; accepting it from the payload
        # would let anyone file a note under someone else's name.
        read_only_fields = ("id", "user", "user_name", "created_at")


class JobPhotoSerializer(TenantModelSerializer):
    user_name = serializers.CharField(source="user.full_name", read_only=True)

    class Meta(TenantModelSerializer.Meta):
        model = JobPhoto
        fields = ("id", "job", "user", "user_name", "image", "caption", "created_at")
        read_only_fields = ("id", "user", "user_name", "created_at")


class JobSerializer(TenantModelSerializer):
    """
    The dispatcher's and cleaner's view of a visit.

    Writes take plain ids for customer/location/service; reads get the nested
    summaries. `price_cents` is optional on write and quoted from the service
    when omitted.
    """

    location_detail = LocationSummarySerializer(source="location", read_only=True)
    customer_detail = CustomerSummarySerializer(source="customer", read_only=True)
    service_detail = ServiceSummarySerializer(source="service", read_only=True)
    assignments = JobAssignmentSerializer(many=True, read_only=True)
    open_time_entry = serializers.SerializerMethodField()
    duration_minutes = serializers.IntegerField(read_only=True)

    class Meta(TenantModelSerializer.Meta):
        model = Job
        fields = (
            "id",
            "organization",
            "customer",
            "customer_detail",
            "location",
            "location_detail",
            "service",
            "service_detail",
            "plan",
            "plan_occurrence",
            "scheduled_start",
            "scheduled_end",
            "duration_minutes",
            "status",
            "status_changed_at",
            "price_cents",
            "notes",
            "cancellation_reason",
            "assignments",
            "open_time_entry",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "organization",
            "plan",
            "plan_occurrence",
            # Status moves through the status action so the state machine is
            # enforced in one place rather than two.
            "status",
            "status_changed_at",
            "cancellation_reason",
            "created_at",
            "updated_at",
        )
        extra_kwargs = {"price_cents": {"required": False}}

    @extend_schema_field(TimeEntrySerializer(allow_null=True))
    def get_open_time_entry(self, obj):
        """The *caller's* open entry, not anyone's -- it drives their clock button."""
        request = self.context.get("request")
        if request is None or not request.user.is_authenticated:
            return None

        entry = next(
            (
                e
                for e in obj.time_entries.all()
                if e.clock_out is None and e.user_id == request.user.id
            ),
            None,
        )
        return TimeEntrySerializer(entry, context=self.context).data if entry else None

    def validate(self, attrs):
        customer = attrs.get("customer", getattr(self.instance, "customer", None))
        location = attrs.get("location", getattr(self.instance, "location", None))
        start = attrs.get("scheduled_start", getattr(self.instance, "scheduled_start", None))
        end = attrs.get("scheduled_end", getattr(self.instance, "scheduled_end", None))

        errors = {}
        if customer and location and location.customer_id != customer.id:
            errors["location"] = "That location belongs to a different customer."
        if start and end and end <= start:
            errors["scheduled_end"] = "A job must end after it starts."
        if errors:
            raise serializers.ValidationError(errors)

        return attrs

    def create(self, validated_data):
        validated_data.setdefault("price_cents", self._quote(validated_data))
        return super().create(validated_data)

    def _quote(self, validated_data) -> int:
        """
        Price an API-created job the same way materialization prices a planned
        one. A missing pricing input surfaces as a 400 naming the field rather
        than a 500 out of the model.
        """
        from scheduling.services import quote_job_price

        service = validated_data["service"]
        location = validated_data["location"]
        start, end = validated_data["scheduled_start"], validated_data["scheduled_end"]
        duration = int((end - start).total_seconds() // 60)

        try:
            return quote_job_price(
                plan=None, service=service, location=location, duration_minutes=duration
            )
        except ValueError as exc:
            field = "square_feet" if "square_feet" in str(exc) else "price_cents"
            raise serializers.ValidationError({field: str(exc)}) from exc


class CustomerJobSerializer(JobSerializer):
    """
    The same job as the customer sees it.

    Drops the crew roster, the dispatcher's internal notes, and the customer's
    own phone number echoed back through `customer_detail` -- none of which a
    customer needs, and the first of which is other people's information.
    """

    class Meta(JobSerializer.Meta):
        fields = tuple(
            field
            for field in JobSerializer.Meta.fields
            if field not in ("assignments", "notes", "open_time_entry")
        )
        read_only_fields = fields


class RecurringPlanSerializer(TenantModelSerializer):
    rrule = serializers.CharField(validators=[validate_rrule_field])
    customer_detail = CustomerSummarySerializer(source="customer", read_only=True)
    location_detail = LocationSummarySerializer(source="location", read_only=True)
    service_detail = ServiceSummarySerializer(source="service", read_only=True)

    class Meta(TenantModelSerializer.Meta):
        model = RecurringPlan
        fields = (
            "id",
            "organization",
            "customer",
            "customer_detail",
            "location",
            "location_detail",
            "service",
            "service_detail",
            "rrule",
            "starts_on",
            "ends_on",
            "preferred_start_time",
            "duration_minutes",
            "price_override_cents",
            "default_assignees",
            "is_active",
            "notes",
            "created_at",
            "updated_at",
        )
        extra_kwargs = {"duration_minutes": {"required": False}}

    def validate(self, attrs):
        customer = attrs.get("customer", getattr(self.instance, "customer", None))
        location = attrs.get("location", getattr(self.instance, "location", None))
        starts_on = attrs.get("starts_on", getattr(self.instance, "starts_on", None))
        ends_on = attrs.get("ends_on", getattr(self.instance, "ends_on", None))

        errors = {}
        if customer and location and location.customer_id != customer.id:
            errors["location"] = "That location belongs to a different customer."
        if starts_on and ends_on and ends_on < starts_on:
            errors["ends_on"] = "The end date cannot fall before the start date."
        if errors:
            raise serializers.ValidationError(errors)

        return attrs

    def validate_default_assignees(self, value):
        """Every default assignee must be staff here -- see services.assert_can_be_assigned."""
        from django.core.exceptions import ValidationError as DjangoValidationError

        from scheduling.services import assert_can_be_assigned

        organization = getattr(self.context.get("request"), "organization", None)
        if organization is None:
            return value

        for user in value:
            try:
                assert_can_be_assigned(user=user, organization=organization)
            except DjangoValidationError as exc:
                raise serializers.ValidationError(
                    f"{user} is not active staff in this organization."
                ) from exc
        return value


# --- Action bodies ---------------------------------------------------------


class AssignSerializer(serializers.Serializer):
    user = serializers.UUIDField()


class StatusChangeSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=JobStatus.choices)
    reason = serializers.CharField(required=False, allow_blank=True, default="")


class OccurrencePreviewSerializer(serializers.Serializer):
    """One upcoming occurrence, in both the forms a UI needs."""

    local = serializers.DateTimeField()
    utc = serializers.DateTimeField()


class PreviewResponseSerializer(serializers.Serializer):
    timezone = serializers.CharField()
    occurrences = OccurrencePreviewSerializer(many=True)


class MaterializeResponseSerializer(serializers.Serializer):
    created = serializers.IntegerField()


class RegenerateResponseSerializer(serializers.Serializer):
    regenerated = serializers.IntegerField()
    kept = serializers.IntegerField()
