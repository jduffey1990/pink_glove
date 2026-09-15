from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema
from rest_framework import filters, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet

from audit.models import ACCESS_WARNING
from audit.serializers import AccessWarningSerializer, RevealRequestSerializer
from audit.services import record_reveal, resolve_reveal_job
from base.permissions import IsDispatcherOrHigher
from base.viewsets import TenantViewSetMixin
from customers.models import Customer, ServiceLocation
from customers.serializers import CustomerSerializer, ServiceLocationSerializer

#: Fields the reveal action returns. Write-only on the normal serializer.
ACCESS_CODE_FIELDS = ("gate_code", "alarm_code", "key_location")


class CustomerViewSet(TenantViewSetMixin, ModelViewSet):
    queryset = Customer.objects.prefetch_related("locations")
    serializer_class = CustomerSerializer
    permission_classes = [IsDispatcherOrHigher]

    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["status", "preferred_contact_method"]
    search_fields = ["first_name", "last_name", "company_name", "email", "phone"]
    ordering_fields = ["last_name", "created_at", "status"]


class ServiceLocationViewSet(TenantViewSetMixin, ModelViewSet):
    queryset = ServiceLocation.objects.select_related("customer")
    serializer_class = ServiceLocationSerializer
    permission_classes = [IsDispatcherOrHigher]

    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["customer", "is_active", "has_pets"]
    # Note: gate_code / alarm_code are encrypted and therefore unsearchable.
    search_fields = ["label", "line1", "city", "postal_code"]
    ordering_fields = ["label", "created_at"]

    def get_permissions(self):
        # Cleaners are not dispatchers, but they are the people standing at the
        # door who need the code. What they get is a reveal, it is logged, and
        # it is bounded by their assignment (ADR-017): a cleaner with no job at
        # this location is refused outright rather than logged and reviewed
        # later, because there is no legitimate reason for that request and a
        # flag cannot undo the exposure.
        if self.action in ("reveal_access", "access_warning"):
            from scheduling.permissions import IsAssignedCleaner

            return [(IsDispatcherOrHigher | IsAssignedCleaner)()]
        return super().get_permissions()

    @extend_schema(
        request=None,
        responses={200: AccessWarningSerializer},
        summary="The warning a user must acknowledge before codes are revealed",
    )
    @action(detail=True, methods=["get"], url_path="access-warning")
    def access_warning(self, request, pk=None):
        """
        The copy the frontend must show before revealing codes.

        Served on its own rather than leaving the client to scrape it out of
        the 400 that an unacknowledged reveal returns. Both put the wording in
        one place, which is the point (ADR-023) -- but a deliberate 400 on a
        healthy path is indistinguishable from a real failure in a browser
        console, a server log, or an error tracker.

        Nothing is logged here: no reveal has happened.
        """
        self.get_object()  # 404s for another organization's location
        return Response({"warning": ACCESS_WARNING})

    @action(detail=True, methods=["post"], url_path="reveal-access")
    def reveal_access(self, request, pk=None):
        """
        Return this location's access codes and record who asked.

        Ordinary schedule data -- address, phone, arrival time -- is not gated
        or logged; a worker needs it constantly. This is only the codes.

        No *timing* restriction is applied here. Whether a reveal sat inside
        its appointment window is decided by the end-of-day pass, because that
        verdict depends on how the schedule finally stood. Blocking on timing
        at request time would strand a worker over a job that got moved.

        What is enforced here is a relationship, not a time: a cleaner needs an
        assignment at this location (ADR-017). Dispatchers and above are
        unrestricted, as they already are for the location record itself.
        """
        location = self.get_object()

        serializer = RevealRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if not serializer.validated_data["acknowledged"]:
            return Response(
                {"detail": ACCESS_WARNING, "acknowledgement_required": True},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Raises a 400 if a dispatcher named a job that is not at this
        # location; binding the record to the wrong visit would be worse.
        job = resolve_reveal_job(
            request=request,
            location=location,
            job_id=serializer.validated_data.get("job"),
        )

        revealed = {field: getattr(location, field) for field in ACCESS_CODE_FIELDS}
        populated = [field for field, value in revealed.items() if value]

        reveal = record_reveal(
            request=request,
            location=location,
            fields_revealed=populated,
            acknowledged=True,
            job=job,
        )

        return Response(
            {
                **revealed,
                "reveal_id": str(reveal.id),
                "job": str(job.id) if job else None,
            }
        )
