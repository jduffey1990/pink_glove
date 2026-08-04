from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters
from rest_framework.viewsets import ModelViewSet

from base.permissions import IsDispatcherOrHigher
from base.viewsets import TenantViewSetMixin
from customers.models import Customer, ServiceLocation
from customers.serializers import CustomerSerializer, ServiceLocationSerializer


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
