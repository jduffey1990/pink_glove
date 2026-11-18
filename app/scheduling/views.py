"""
The scheduling API.

Views stay thin. Anything that decides something -- what a status may become,
whether a user may be assigned, what a plan edit does to existing jobs -- lives
in `scheduling.services`, because the seed command and the beat tasks need the
same answers and a rule implemented in a view is a rule only the API obeys.

Every viewset rides `TenantViewSetMixin`, so the organization comes from the
resolved tenant and never from request data.
"""

import datetime as dt

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import filters
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import ModelViewSet

from app.exceptions import ConflictError
from base.exports import AnyAcceptNegotiation, csv_response
from base.permissions import IsAdminOrHigher, IsDispatcherOrHigher, IsOrgMember, IsStaff
from base.serializers import DetailSerializer
from base.viewsets import TenantViewSetMixin
from scheduling.filters import JobFilterSet, TimeEntryFilterSet
from scheduling.models import Job, JobNote, JobPhoto, RecurringPlan, TimeEntry, assigned_to
from scheduling.permissions import IsAssignedCleaner
from scheduling.reports import TIMESHEET_CSV_HEADER, timesheet, timesheet_csv_rows
from scheduling.serializers import (
    AssignSerializer,
    CustomerJobSerializer,
    JobNoteSerializer,
    JobPhotoSerializer,
    JobSerializer,
    MaterializeResponseSerializer,
    PreviewResponseSerializer,
    RecurringPlanSerializer,
    RegenerateResponseSerializer,
    StatusChangeSerializer,
    TimeEntrySerializer,
    TimesheetQuerySerializer,
    TimesheetSerializer,
)
from scheduling.services import (
    assign_user,
    claim_on,
    clock_in,
    clock_out,
    delete_refusal,
    edit_refusal,
    expand_occurrences,
    materialize_plan,
    promote_lead,
    regenerate_plan,
    transition_job,
    unassign_user,
)
from users.enums import DISPATCHER_ROLES, Role

#: Plan fields whose change invalidates already-materialized future jobs.
#: Editing a note or a name does not; editing the rule or the time does.
REGENERATING_FIELDS = frozenset(
    {
        "rrule",
        "starts_on",
        "ends_on",
        "preferred_start_time",
        "duration_minutes",
        "location",
        "service",
        "price_override_cents",
        "is_active",
    }
)


class RecurringPlanViewSet(TenantViewSetMixin, ModelViewSet):
    """Standing appointments. Dispatcher and above."""

    queryset = RecurringPlan.objects.select_related(
        "customer", "location", "service", "organization"
    ).prefetch_related("default_assignees")
    serializer_class = RecurringPlanSerializer
    permission_classes = [IsDispatcherOrHigher]

    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["customer", "location", "is_active"]
    ordering_fields = ["created_at", "starts_on"]

    def perform_create(self, serializer):
        """
        Materialize straight away rather than waiting for tonight's beat run.
        A dispatcher who creates a plan expects to see the visits appear.

        One transaction for the plan, its visits and the customer's promotion
        from Lead (ADR-037): a plan whose visits cannot be priced is a 400, and
        a 400 leaves nothing behind -- not the plan, and not an Active customer
        with nothing booked.
        """
        try:
            with transaction.atomic():
                super().perform_create(serializer)
                materialize_plan(serializer.instance)
                if serializer.instance.is_active:
                    promote_lead(serializer.instance.customer)
        except ValueError as exc:
            raise DjangoValidationError({"service": str(exc)}) from exc

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        """
        Edit the plan, then reconcile the jobs it already created.

        The response carries `regenerated` and `kept` so the dispatcher can see
        what happened to the eight weeks of visits already on the board --
        which is the whole point of ADR-020's rule.

        A paused plan made active, or an active plan handed to another
        customer, is a booking for whoever it now belongs to (ADR-037). One
        transaction for the edit, the regeneration and the promotion, so a
        regeneration refused as a 400 leaves neither the edit nor the status.
        """
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        before = {field: getattr(instance, field) for field in REGENERATING_FIELDS}
        customer_before = instance.customer_id

        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)

        instance.refresh_from_db()
        changed = any(getattr(instance, field) != before[field] for field in REGENERATING_FIELDS)

        body = serializer.data
        if changed:
            try:
                body = {**body, **regenerate_plan(instance)}
            except ValueError as exc:
                raise DjangoValidationError({"service": str(exc)}) from exc

        booked = instance.is_active and (
            not before["is_active"] or instance.customer_id != customer_before
        )
        if booked:
            promote_lead(instance.customer)

        return Response(body)

    @extend_schema(
        request=None,
        responses={200: PreviewResponseSerializer},
        summary="Preview the next occurrences without saving them",
    )
    @action(detail=True, methods=["get"])
    def preview(self, request, pk=None):
        """
        The next N occurrences, in local and UTC form.

        This is what lets the UI show "so: Tue Sep 16, Sep 23, Sep 30..." while
        someone types an RRULE, instead of making them save and find out.
        """
        plan = self.get_object()
        try:
            count = max(1, min(int(request.query_params.get("count", 6)), 50))
        except (TypeError, ValueError):
            count = 6

        tz = plan.organization.tz
        window_start = max(plan.organization.today(), plan.starts_on)

        # Widen the window until enough occurrences turn up or a year is spent:
        # a monthly rule needs a much longer reach than a weekly one.
        occurrences: list[dt.datetime] = []
        for days in (56, 180, 400):
            occurrences = expand_occurrences(
                plan,
                window_start=window_start,
                window_end=window_start + dt.timedelta(days=days),
            )
            if len(occurrences) >= count:
                break

        return Response(
            {
                "timezone": plan.organization.timezone,
                "occurrences": [
                    {"local": o.astimezone(tz).isoformat(), "utc": o.isoformat()}
                    for o in occurrences[:count]
                ],
            }
        )

    @extend_schema(
        request=None,
        responses={200: MaterializeResponseSerializer, 400: DetailSerializer},
        summary="Create this plan's jobs out to the horizon now",
    )
    @action(detail=True, methods=["post"])
    def materialize(self, request, pk=None):
        plan = self.get_object()
        try:
            created = materialize_plan(plan)
        except ValueError as exc:
            raise DjangoValidationError({"service": str(exc)}) from exc

        return Response({"created": created})

    @extend_schema(
        request=None,
        responses={200: RegenerateResponseSerializer},
        summary="Rebuild untouched future jobs from the current definition",
    )
    @action(detail=True, methods=["post"])
    def regenerate(self, request, pk=None):
        plan = self.get_object()
        try:
            return Response(regenerate_plan(plan))
        except ValueError as exc:
            raise DjangoValidationError({"service": str(exc)}) from exc


class IsCustomerOfJob(IsOrgMember):
    """
    A customer reading their own visits.

    Which jobs those are is decided by `JobViewSet.get_queryset`, not here:
    this only says the caller is a customer of this organization. Composed as
    `IsStaff | IsCustomerOfJob` so staff keep their own, wider access.
    """

    message = "This is only available within your organization."

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        membership = getattr(request, "membership", None)
        return membership is not None and membership.role == Role.CUSTOMER


class JobViewSet(TenantViewSetMixin, ModelViewSet):
    """
    Visits.

    The queryset is role-scoped on top of the tenant scope: a cleaner sees the
    jobs they are on, a customer sees their own, a dispatcher sees everything.
    """

    queryset = Job.objects.select_related(
        "customer", "location", "service", "organization", "plan"
    ).prefetch_related("assignments__user", "time_entries")
    serializer_class = JobSerializer

    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_class = JobFilterSet
    ordering_fields = ["scheduled_start", "status", "created_at"]
    ordering = ["scheduled_start"]

    def get_permissions(self):
        if self.action in ("create", "update", "partial_update", "destroy"):
            return [IsDispatcherOrHigher()]
        if self.action in ("assign", "unassign"):
            return [IsDispatcherOrHigher()]
        if self.action in ("set_status", "clock_in_action", "clock_out_action"):
            return [(IsDispatcherOrHigher | IsAssignedCleaner)()]
        return [(IsStaff | IsCustomerOfJob)()]

    def get_serializer_class(self):
        if self._role() == Role.CUSTOMER:
            return CustomerJobSerializer
        return JobSerializer

    def _role(self) -> str | None:
        membership = getattr(self.request, "membership", None)
        return membership.role if membership else None

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user

        if user.is_superuser:
            return queryset

        role = self._role()
        if role in DISPATCHER_ROLES:
            return queryset
        if role == Role.CLEANER:
            return queryset.filter(assigned_to(user)).distinct()
        if role == Role.CUSTOMER:
            return queryset.filter(customer__user=user)

        return queryset.none()

    def perform_update(self, serializer):
        """
        A finished visit is not edited in place; it is reopened first. The
        rule is `services.edit_refusal`, which is also what the job publishes
        as `available_actions`, so the page and the server cannot disagree.
        """
        refusal = edit_refusal(serializer.instance, claim=claim_on(serializer.instance))
        if refusal:
            raise ConflictError(refusal, {"status": serializer.instance.status})
        super().perform_update(serializer)

    def perform_destroy(self, instance):
        """
        Deleting is for a visit booked by mistake. One that was worked, is
        under way or finished, or came from a plan is cancelled instead: the
        row and the reason stay (ADR-021). See `services.delete_refusal`.
        """
        refusal = delete_refusal(instance, claim=claim_on(instance))
        if refusal:
            raise ConflictError(refusal, {"status": instance.status})
        instance.delete()

    @extend_schema(
        request=AssignSerializer,
        responses={200: JobSerializer, 400: DetailSerializer, 409: DetailSerializer},
        summary="Put a cleaner on this job",
    )
    @action(detail=True, methods=["post"])
    def assign(self, request, pk=None):
        job = self.get_object()
        serializer = AssignSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = self._staff_user(serializer.validated_data["user"])

        assign_user(job=job, user=user, assigned_by=request.user)

        job.refresh_from_db()
        return Response(self.get_serializer(job).data)

    @extend_schema(
        request=AssignSerializer,
        responses={200: JobSerializer, 400: DetailSerializer},
        summary="Take a cleaner off this job",
    )
    @action(detail=True, methods=["post"])
    def unassign(self, request, pk=None):
        job = self.get_object()
        serializer = AssignSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = self._staff_user(serializer.validated_data["user"])
        unassign_user(job=job, user=user)

        job.refresh_from_db()
        return Response(self.get_serializer(job).data)

    def _staff_user(self, user_id):
        """
        Resolve a user id to a user, without confirming whether an id we will
        refuse anyway belongs to a real account elsewhere.
        """
        from users.models import CustomUser

        user = CustomUser.objects.filter(pk=user_id).first()
        if user is None:
            raise DjangoValidationError({"user": "No such user."})
        return user

    @extend_schema(
        request=StatusChangeSerializer,
        responses={200: JobSerializer, 400: DetailSerializer, 409: DetailSerializer},
        summary="Move this job to another status",
        description=(
            "Transitions are enforced server-side. A refusal returns 409 with "
            "the allowed next states, so a client can render the right buttons "
            "without a second request."
        ),
    )
    @action(detail=True, methods=["post"], url_path="status", url_name="status")
    def set_status(self, request, pk=None):
        job = self.get_object()
        serializer = StatusChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        transition_job(
            job=job,
            to_status=serializer.validated_data["status"],
            actor=request.user,
            reason=serializer.validated_data.get("reason", ""),
        )

        job.refresh_from_db()
        return Response(self.get_serializer(job).data)

    @extend_schema(
        request=None,
        responses={200: JobSerializer, 409: DetailSerializer},
        summary="Start the clock on this job",
    )
    @action(detail=True, methods=["post"], url_path="clock-in", url_name="clock-in")
    def clock_in_action(self, request, pk=None):
        job = self.get_object()
        clock_in(job=job, user=request.user)

        job.refresh_from_db()
        return Response(self.get_serializer(job).data)

    @extend_schema(
        request=None,
        responses={200: JobSerializer, 409: DetailSerializer},
        summary="Stop the clock on this job",
    )
    @action(detail=True, methods=["post"], url_path="clock-out", url_name="clock-out")
    def clock_out_action(self, request, pk=None):
        job = self.get_object()
        clock_out(job=job, user=request.user)

        job.refresh_from_db()
        return Response(self.get_serializer(job).data)


class TimeEntryViewSet(TenantViewSetMixin, ModelViewSet):
    """
    Recorded hours. Staff read; dispatchers correct.

    Creation goes through the job's clock-in action, not here -- that is where
    the "one open entry" rule and the status side effect live.
    """

    queryset = TimeEntry.objects.select_related("job", "user", "organization")
    serializer_class = TimeEntrySerializer
    http_method_names = ["get", "patch", "head", "options"]

    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_class = TimeEntryFilterSet
    ordering_fields = ["clock_in", "created_at"]

    def get_permissions(self):
        if self.request.method in ("GET", "HEAD", "OPTIONS"):
            return [IsStaff()]
        return [IsDispatcherOrHigher()]

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user

        if user.is_superuser:
            return queryset

        membership = getattr(self.request, "membership", None)
        role = membership.role if membership else None
        if role in DISPATCHER_ROLES:
            return queryset
        if role == Role.CLEANER:
            return queryset.filter(user=user)

        return queryset.none()


class JobRelatedViewSet(TenantViewSetMixin, ModelViewSet):
    """
    Shared behaviour for notes and photos: both hang off a job, both stamp
    `user` from the request, and both are readable by the crew on that job.
    """

    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["job"]
    ordering_fields = ["created_at"]

    def get_permissions(self):
        return [(IsDispatcherOrHigher | IsAssignedCleaner)()]

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user

        if user.is_superuser:
            return queryset

        membership = getattr(self.request, "membership", None)
        role = membership.role if membership else None
        if role in DISPATCHER_ROLES:
            return queryset
        if role == Role.CLEANER:
            return queryset.filter(assigned_to(user, via="job__")).distinct()

        # Customers cannot reach either of these at all.
        return queryset.none()

    def perform_create(self, serializer):
        job = serializer.validated_data["job"]
        self._assert_may_write(job)
        serializer.save(organization=self.get_organization(), user=self.request.user)

    def _assert_may_write(self, job):
        user = self.request.user
        if user.is_superuser:
            return

        membership = getattr(self.request, "membership", None)
        role = membership.role if membership else None
        if role in DISPATCHER_ROLES:
            return

        if not job.assignments.filter(user=user).exists():
            raise PermissionDenied("You are not assigned to that job.")

    def _assert_author_or_dispatcher(self, instance, verb):
        """The author, or a dispatcher. Not any cleaner who can see it."""
        user = self.request.user
        membership = getattr(self.request, "membership", None)
        role = membership.role if membership else None

        if not (user.is_superuser or role in DISPATCHER_ROLES or instance.user_id == user.id):
            raise PermissionDenied(f"You can only {verb} your own notes and photos.")

    def perform_update(self, serializer):
        self._assert_author_or_dispatcher(serializer.instance, "edit")

        # A note is evidence about one visit. Re-pointing it would move it past
        # the assignment check that `perform_create` ran against the first job.
        job = serializer.validated_data.get("job")
        if job is not None and job.pk != serializer.instance.job_id:
            raise ValidationError({"job": "This cannot be moved to another job."})

        super().perform_update(serializer)

    def perform_destroy(self, instance):
        self._assert_author_or_dispatcher(instance, "delete")
        instance.delete()


class JobNoteViewSet(JobRelatedViewSet):
    queryset = JobNote.objects.select_related("job", "user", "organization")
    serializer_class = JobNoteSerializer


class JobPhotoViewSet(JobRelatedViewSet):
    queryset = JobPhoto.objects.select_related("job", "user", "organization")
    serializer_class = JobPhotoSerializer


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
# Owner and admin only. What a person was paid for is payroll, and a
# dispatcher runs the board without it. The organization comes from the
# resolved tenant like everywhere else; `scheduling.reports` takes it as an
# argument and reads nothing from the request.


def _timesheet_for(request) -> dict:
    query = TimesheetQuerySerializer(data=request.query_params)
    query.is_valid(raise_exception=True)
    return timesheet(request.organization, **query.validated_data)


class TimesheetReportView(APIView):
    """Hours worked per staff member over a period of organization-local days."""

    permission_classes = [IsAdminOrHigher]

    @extend_schema(
        parameters=[TimesheetQuerySerializer],
        responses={200: TimesheetSerializer, 400: DetailSerializer, 403: DetailSerializer},
        summary="Hours worked per staff member in a period",
        description=(
            "An entry is filed under the organization-local day it was clocked in "
            "on, whole, however late it ran. An entry still open is listed and "
            "flagged and adds nothing to a total."
        ),
    )
    def get(self, request):
        return Response(TimesheetSerializer(_timesheet_for(request)).data)


class TimesheetCSVView(APIView):
    """The same timesheet as a file, one row per entry."""

    permission_classes = [IsAdminOrHigher]
    content_negotiation_class = AnyAcceptNegotiation

    @extend_schema(
        parameters=[TimesheetQuerySerializer],
        responses={
            (200, "text/csv"): OpenApiTypes.STR,
            400: DetailSerializer,
            403: DetailSerializer,
        },
        summary="The timesheet as CSV",
        description=(
            "One row per entry. `seconds` is exact; `hours_decimal` is the same "
            "figure in hours, rounded half up to two places. Text cells that a "
            "spreadsheet would run as a formula are prefixed with an apostrophe."
        ),
    )
    def get(self, request):
        report = _timesheet_for(request)
        return csv_response(
            f"timesheet_{report['date_from']}_{report['date_to']}.csv",
            TIMESHEET_CSV_HEADER,
            timesheet_csv_rows(report, request.organization.tz),
        )
