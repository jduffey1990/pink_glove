from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet

from audit.models import ACCESS_WARNING
from audit.serializers import RevealRequestSerializer
from audit.services import record_reveal
from base.permissions import IsDispatcherOrHigher, IsStaff
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
        # door who need the code. The reveal is what they get, and it is logged.
        if self.action == "reveal_access":
            return [IsStaff()]
        return super().get_permissions()

    @action(detail=True, methods=["post"], url_path="reveal-access")
    def reveal_access(self, request, pk=None):
        """
        Return this location's access codes and record who asked.

        Ordinary schedule data -- address, phone, arrival time -- is not gated
        or logged; a worker needs it constantly. This is only the codes.

        No time restriction is applied here. Whether a reveal sat inside its
        appointment window is decided by an end-of-day pass (Phase 3), because
        that verdict depends on how the schedule finally stood. Blocking at
        request time would strand a worker over a job that got moved.
        """
        location = self.get_object()

        serializer = RevealRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if not serializer.validated_data["acknowledged"]:
            return Response(
                {"detail": ACCESS_WARNING, "acknowledgement_required": True},
                status=status.HTTP_400_BAD_REQUEST,
            )

        revealed = {field: getattr(location, field) for field in ACCESS_CODE_FIELDS}
        populated = [field for field, value in revealed.items() if value]

        reveal = record_reveal(
            request=request,
            location=location,
            fields_revealed=populated,
            acknowledged=True,
        )

        return Response({**revealed, "reveal_id": str(reveal.id)})
