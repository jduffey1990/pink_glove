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
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema
from rest_framework import filters, status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet

from base.permissions import IsDispatcherOrHigher, IsOrgMember, IsStaff
from base.serializers import DetailSerializer
from base.viewsets import TenantViewSetMixin
from scheduling.filters import JobFilterSet, TimeEntryFilterSet
from scheduling.models import Job, JobNote, JobPhoto, RecurringPlan, TimeEntry
from scheduling.permissions import IsAssignedCleaner
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
)
from scheduling.services import (
    ConflictError,
    assign_user,
    clock_in,
    clock_out,
    expand_occurrences,
    materialize_plan,
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


def _conflict(exc: ConflictError) -> Response:
    return Response({"detail": exc.detail, **exc.payload}, status=status.HTTP_409_CONFLICT)


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
        """
        super().perform_create(serializer)
        try:
            materialize_plan(serializer.instance)
        except ValueError as exc:
            raise DjangoValidationError({"service": str(exc)}) from exc

    def update(self, request, *args, **kwargs):
        """
        Edit the plan, then reconcile the jobs it already created.

        The response carries `regenerated` and `kept` so the dispatcher can see
        what happened to the eight weeks of visits already on the board --
        which is the whole point of ADR-020's rule.
        """
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        before = {field: getattr(instance, field) for field in REGENERATING_FIELDS}

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
        today = timezone.now().astimezone(tz).date()
        window_start = max(today, plan.starts_on)

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
            return queryset.filter(assignments__user=user).distinct()
        if role == Role.CUSTOMER:
            return queryset.filter(customer__user=user)

        return queryset.none()

    def perform_destroy(self, instance):
        """
        A job somebody worked is history, not a mistake. Cancelling records the
        reason and keeps the row; deleting it would orphan the time entries and
        the hours billed against them.
        """
        if instance.time_entries.exists():
            raise ConflictError(
                "This job has recorded time. Cancel it instead of deleting it.",
                {"status": instance.status},
            )
        instance.delete()

    def destroy(self, request, *args, **kwargs):
        try:
            return super().destroy(request, *args, **kwargs)
        except ConflictError as exc:
            return _conflict(exc)

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

        try:
            assign_user(job=job, user=user, assigned_by=request.user)
        except ConflictError as exc:
            return _conflict(exc)

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

        try:
            transition_job(
                job=job,
                to_status=serializer.validated_data["status"],
                actor=request.user,
                reason=serializer.validated_data.get("reason", ""),
            )
        except ConflictError as exc:
            return _conflict(exc)

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
        try:
            clock_in(job=job, user=request.user)
        except ConflictError as exc:
            return _conflict(exc)

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
        try:
            clock_out(job=job, user=request.user)
        except ConflictError as exc:
            return _conflict(exc)

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
            return queryset.filter(job__assignments__user=user).distinct()

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

        serializer.save()

    def perform_destroy(self, instance):
        self._assert_author_or_dispatcher(instance, "delete")
        instance.delete()


class JobNoteViewSet(JobRelatedViewSet):
    queryset = JobNote.objects.select_related("job", "user", "organization")
    serializer_class = JobNoteSerializer


class JobPhotoViewSet(JobRelatedViewSet):
    queryset = JobPhoto.objects.select_related("job", "user", "organization")
    serializer_class = JobPhotoSerializer
