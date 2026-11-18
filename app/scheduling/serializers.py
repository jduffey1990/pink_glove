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

import datetime as dt

from django.db import models, transaction
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from base.serializers import DateRangeQuerySerializer
from base.viewsets import TenantModelSerializer
from catalog.models import Service
from customers.models import ServiceLocation
from customers.serializers import CustomerSummarySerializer
from scheduling.enums import REASON_REQUIRED_STATUSES, JobStatus
from scheduling.filters import MAX_RANGE_DAYS
from scheduling.models import (
    Job,
    JobAssignment,
    JobNote,
    JobPhoto,
    RecurringPlan,
    TimeEntry,
)
from scheduling.models import validate_rrule as _validate_rrule
from scheduling.services import (
    ARCHIVED_CUSTOMER_REFUSAL,
    activate_booked_customer,
    job_actions,
    job_claims,
    next_statuses,
    refuses_booking,
)
from users.enums import DISPATCHER_ROLES, Role


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


class NextStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=JobStatus.choices)
    reason_required = serializers.BooleanField()


class JobListSerializer(serializers.ListSerializer):
    """
    A page of jobs.

    Exists for one reason: whether another app has a claim on a visit
    (ADR-033) is asked once for the page, not once per row. The child is
    handed the page before it serializes any of it.
    """

    def to_representation(self, data):
        jobs = list(data.all() if isinstance(data, models.Manager) else data)
        self.child.page = jobs
        return super().to_representation(jobs)


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
    is_terminal = serializers.BooleanField(read_only=True)
    next_statuses = serializers.SerializerMethodField()
    available_actions = serializers.SerializerMethodField()
    assignees = serializers.ListField(
        child=serializers.UUIDField(),
        write_only=True,
        required=False,
        help_text=(
            "User ids to put on a new job, so booking a visit with its crew is "
            "one request that either wholly happens or does not. Create only: "
            "the crew of an existing job changes through assign and unassign."
        ),
    )

    #: The jobs being serialized alongside this one, set by `JobListSerializer`.
    page: list = []

    class Meta(TenantModelSerializer.Meta):
        model = Job
        list_serializer_class = JobListSerializer
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
            "is_terminal",
            "next_statuses",
            "available_actions",
            "price_cents",
            "notes",
            "cancellation_reason",
            "assignments",
            "assignees",
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

    @extend_schema_field(NextStatusSerializer(many=True))
    def get_next_statuses(self, obj):
        """
        The moves *this caller* may make, so the frontend draws its buttons
        from the server's state machine instead of keeping a copy (ADR-023).
        Read off the request's membership: no query per row on a list.
        """
        is_dispatcher, role = self._caller()
        if not is_dispatcher and role != Role.CLEANER:
            return []

        # Only a dispatcher can reopen, and only reopening can be held.
        claim = self._claim(obj) if is_dispatcher and obj.is_terminal else None

        return [
            {"status": status, "reason_required": status in REASON_REQUIRED_STATUSES}
            for status in next_statuses(obj, is_dispatcher=is_dispatcher, claim=claim)
        ]

    def get_available_actions(self, obj) -> list[str]:
        """
        What this caller may do to the visit besides move its status: `edit`,
        `delete`, both or neither. The server refuses anything not listed.
        """
        # Decided by the functions `JobViewSet` enforces with, so the page and
        # the server cannot drift (ADR-023, ADR-032). They read the prefetched
        # time entries: no query per row on a list.
        is_dispatcher, _ = self._caller()
        claim = self._claim(obj) if is_dispatcher else None
        return job_actions(obj, is_dispatcher=is_dispatcher, claim=claim)

    def _claim(self, obj) -> str | None:
        """
        Another app's hold on this visit, if any (ADR-033).

        Asked once for the whole page when there is one, and remembered, so
        the two fields that read it and every row of a list share one lookup.
        """
        cache = self.__dict__.setdefault("_claims", {})
        if obj.pk not in cache:
            jobs = self.page if obj in self.page else [obj]
            claims = job_claims(jobs)
            cache.update({job.pk: claims.get(job.pk) for job in jobs})
        return cache[obj.pk]

    def _caller(self) -> tuple[bool, str | None]:
        """(dispatcher or higher, role) of whoever is asking, off the request."""
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False, None

        # A deactivated organization is read-only: `IsOrgMember` refuses every
        # write from anyone but a superuser (ADR-028). So nothing is offered
        # there either, or the page would draw buttons that can only 403.
        organization = getattr(request, "organization", None)
        if organization is not None and not organization.is_active and not user.is_superuser:
            return False, None

        role = getattr(getattr(request, "membership", None), "role", None)
        return user.is_superuser or role in DISPATCHER_ROLES, role

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
        # Booking is a new visit or one handed to another customer; editing
        # an archived customer's existing visit is not (ADR-037).
        booked = self.instance is None or customer.id != self.instance.customer_id
        if booked and refuses_booking(customer):
            errors["customer"] = ARCHIVED_CUSTOMER_REFUSAL
        if customer and location and location.customer_id != customer.id:
            errors["location"] = "That location belongs to a different customer."
        if start and end and end <= start:
            errors["scheduled_end"] = "A job must end after it starts."
        if errors:
            raise serializers.ValidationError(errors)

        return attrs

    def validate_assignees(self, value):
        if self.instance is not None:
            raise serializers.ValidationError(
                "The crew of an existing job is changed with the assign and unassign actions."
            )
        # Order kept, repeats dropped: naming somebody twice is not an error.
        return list(dict.fromkeys(value))

    @transaction.atomic
    def create(self, validated_data):
        assignee_ids = validated_data.pop("assignees", [])
        validated_data.setdefault("price_cents", self._quote(validated_data))

        job = super().create(validated_data)
        self._assign(job, assignee_ids)
        # Last, so a refused crew rolls the booking back before it promotes.
        activate_booked_customer(job.customer)
        return job

    @transaction.atomic
    def update(self, instance, validated_data):
        """
        A visit handed to another customer is booked for them (ADR-037): the
        new customer is promoted, in the edit's transaction. The old one is
        left as they are.
        """
        customer_before = instance.customer_id
        job = super().update(instance, validated_data)
        if job.customer_id != customer_before:
            activate_booked_customer(job.customer)
        return job

    def _assign(self, job, user_ids) -> None:
        """
        Put the chosen crew on a job that was just created.

        Through `assign_user`, the one sanctioned way to create an assignment,
        so the staff-of-this-organization check is not restated here. A refusal
        raises out of `create`'s transaction and takes the job with it: a
        visit is never left booked with half the crew that was asked for.
        """
        from django.core.exceptions import ValidationError as DjangoValidationError

        from scheduling.services import assign_user
        from users.models import CustomUser

        if not user_ids:
            return

        users = CustomUser.objects.in_bulk(user_ids)
        assigned_by = getattr(self.context.get("request"), "user", None)

        # A missing id is refused in the same words as somebody else's cleaner,
        # so the answer does not say which accounts exist.
        refused = serializers.ValidationError(
            {"assignees": "One of those people is not active staff in this organization."}
        )

        for user_id in user_ids:
            user = users.get(user_id)
            if user is None:
                raise refused
            try:
                assign_user(job=job, user=user, assigned_by=assigned_by)
            except DjangoValidationError as exc:
                raise refused from exc

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
            if field not in ("assignments", "assignees", "notes", "open_time_entry")
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
        # An active plan books an archived customer's work every night, so it
        # is not saved active; pausing one is how it is stopped and stays
        # allowed. A paused plan is not handed to an archived customer either
        # (ADR-037).
        is_active = attrs.get("is_active", getattr(self.instance, "is_active", True))
        handed_over = self.instance is not None and customer.id != self.instance.customer_id
        if refuses_booking(customer) and (is_active or handed_over):
            errors["customer"] = ARCHIVED_CUSTOMER_REFUSAL
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


# --- Reports ---------------------------------------------------------------
# Shapes for `scheduling.reports`. Plain serializers over plain dicts: a report
# is computed, not stored, and these exist so the frontend reads generated
# types rather than guessing (ADR-019).


class TimesheetQuerySerializer(DateRangeQuerySerializer):
    #: The cap the schedule itself uses. A pay period is a week, a fortnight
    #: or a month; two months leaves room for one that straddles.
    max_days = MAX_RANGE_DAYS


class TimesheetEntrySerializer(serializers.Serializer):
    id = serializers.UUIDField()
    job = serializers.UUIDField()
    customer_name = serializers.CharField()
    local_date = serializers.DateField(
        help_text=(
            "The organization-local day this entry is filed under: the day of "
            "its clock-in, however late it ran (ADR-030)."
        )
    )
    clock_in = serializers.DateTimeField(default_timezone=dt.UTC)
    clock_out = serializers.DateTimeField(default_timezone=dt.UTC, allow_null=True)
    seconds = serializers.IntegerField(
        allow_null=True, help_text="Elapsed seconds. Null while the entry is open, never zero."
    )
    is_open = serializers.BooleanField()


class TimesheetStaffSerializer(serializers.Serializer):
    user = serializers.UUIDField()
    name = serializers.CharField()
    total_seconds = serializers.IntegerField(help_text="Closed entries only.")
    entry_count = serializers.IntegerField(help_text="Every entry listed, open ones included.")
    open_entry_count = serializers.IntegerField()
    entries = TimesheetEntrySerializer(many=True)


class TimesheetSerializer(serializers.Serializer):
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    timezone = serializers.CharField(help_text="The zone the period's days were reckoned in.")
    total_seconds = serializers.IntegerField(help_text="Closed entries only.")
    entry_count = serializers.IntegerField()
    open_entry_count = serializers.IntegerField()
    staff = TimesheetStaffSerializer(many=True)
