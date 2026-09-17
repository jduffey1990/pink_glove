from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema
from rest_framework import filters, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet

from base.permissions import IsAdminOrHigher, IsStaff
from base.serializers import DetailSerializer
from base.viewsets import TenantViewSetMixin
from catalog.models import Service
from catalog.serializers import QuoteResultSerializer, QuoteSerializer, ServiceSerializer


class ServiceViewSet(TenantViewSetMixin, ModelViewSet):
    queryset = Service.objects.all()
    serializer_class = ServiceSerializer

    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["pricing_model", "is_active"]
    search_fields = ["name", "description"]
    ordering_fields = ["name", "base_price_cents", "created_at"]

    def get_permissions(self):
        # Cleaners need to read the catalog to know what a job involves;
        # changing prices is an admin decision.
        permission_classes = (
            [IsStaff] if self.request.method in ("GET", "HEAD") else [IsAdminOrHigher]
        )
        return [permission() for permission in permission_classes]

    @extend_schema(
        request=QuoteSerializer,
        responses={200: QuoteResultSerializer, 400: DetailSerializer},
        summary="Price this service for a given size or duration",
    )
    @action(detail=True, methods=["post"], serializer_class=QuoteSerializer)
    def quote(self, request, pk=None):
        """Price this service for a given size or duration."""
        service = self.get_object()
        serializer = QuoteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            cents = service.quote_cents(**serializer.validated_data)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {
                "service": service.name,
                "pricing_model": service.pricing_model,
                "amount_cents": cents,
            }
        )
