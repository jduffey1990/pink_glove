"""
The billing API.

Dispatcher and above throughout. Cleaners and customers get a 403: a cleaner
has no reason to see what a house is billed, and the customer's own view of
their invoices is Phase 5, where it will be a different, narrower serializer.

Views stay thin. Every rule -- what may be invoiced, what a draft becomes when
it is issued, whether a payment fits -- lives in `billing.services`, because
the seed command and the email task need the same answers. A `ConflictError`
raised down there renders as a 409 through `app.exceptions`, so nothing here
catches one.
"""

from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema
from rest_framework import filters
from rest_framework.decorators import action
from rest_framework.generics import get_object_or_404
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet

from base.permissions import IsDispatcherOrHigher
from base.serializers import DetailSerializer
from base.viewsets import TenantViewSetMixin
from billing import services
from billing.filters import InvoiceFilterSet, PaymentFilterSet
from billing.models import Invoice, InvoiceLine, Payment
from billing.serializers import (
    BillableJobSerializer,
    DraftInvoiceSerializer,
    InvoiceLineSerializer,
    InvoiceSerializer,
    PaymentSerializer,
    ReasonSerializer,
    RecordPaymentSerializer,
)
from customers.models import Customer


class InvoiceViewSet(TenantViewSetMixin, ModelViewSet):
    """
    Invoices. Dispatcher and above.

    Creation does not take a body of lines: it takes a customer and a list of
    visits, and `services.draft_invoice` builds the lines from them. That is
    the only way a line can carry a price nobody typed.
    """

    queryset = (
        Invoice.objects.select_related("customer", "organization")
        .prefetch_related("lines", "payments__recorded_by")
        .all()
    )
    serializer_class = InvoiceSerializer
    permission_classes = [IsDispatcherOrHigher]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_class = InvoiceFilterSet
    ordering_fields = ["created_at", "issued_on", "due_on", "number"]
    ordering = ["-created_at"]

    def get_serializer_class(self):
        if self.action == "create":
            return DraftInvoiceSerializer
        return InvoiceSerializer

    @extend_schema(
        request=DraftInvoiceSerializer,
        responses={201: InvoiceSerializer, 400: DetailSerializer, 409: DetailSerializer},
        summary="Open a draft invoice over a customer's uninvoiced visits",
    )
    def create(self, request, *args, **kwargs):
        serializer = DraftInvoiceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        organization = self.get_organization()
        customer = get_object_or_404(
            Customer.objects.filter(organization=organization),
            pk=serializer.validated_data["customer"],
        )

        invoice = services.draft_invoice(
            customer=customer, jobs=serializer.validated_data["jobs"], actor=request.user
        )

        return Response(
            InvoiceSerializer(invoice, context=self.get_serializer_context()).data, status=201
        )

    def update(self, request, *args, **kwargs):
        """Only `notes`, and only while it is a draft (ADR-026)."""
        invoice = self.get_object()
        services.assert_draft(invoice, "edit")
        return super().update(request, *args, **kwargs)

    def perform_destroy(self, instance):
        """
        A draft may be thrown away; anything issued is voided instead. The
        model raises for the second case, which renders as a 400 naming why.
        """
        instance.delete()

    @extend_schema(
        request=None,
        responses={200: InvoiceSerializer, 409: DetailSerializer},
        summary="Number the invoice and freeze what it says",
    )
    @action(detail=True, methods=["post"])
    def issue(self, request, pk=None):
        invoice = services.issue_invoice(self.get_object(), actor=request.user)
        return Response(self.get_serializer(invoice).data)

    @extend_schema(
        request=ReasonSerializer,
        responses={200: InvoiceSerializer, 400: DetailSerializer, 409: DetailSerializer},
        summary="Void an issued invoice, freeing its visits",
    )
    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        body = ReasonSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        invoice = services.void_invoice(
            self.get_object(), actor=request.user, reason=body.validated_data["reason"]
        )
        return Response(self.get_serializer(invoice).data)

    @extend_schema(
        request=None,
        responses={202: InvoiceSerializer, 400: DetailSerializer, 409: DetailSerializer},
        summary="Email the invoice to its bill-to address",
        description=(
            "Queued, not sent inline. `sent_at` is stamped by the worker once "
            "the mail is away, so re-read the invoice to see it."
        ),
    )
    @action(detail=True, methods=["post"])
    def send(self, request, pk=None):
        invoice = services.send_invoice(self.get_object(), actor=request.user)
        invoice.refresh_from_db()
        return Response(self.get_serializer(invoice).data, status=202)

    @extend_schema(
        responses={200: BillableJobSerializer(many=True)},
        summary="Visits that are finished and on no live invoice",
    )
    @action(detail=False, methods=["get"], url_path="billable-jobs", url_name="billable-jobs")
    def billable_jobs(self, request):
        organization = self.get_organization()

        customer = None
        customer_id = request.query_params.get("customer")
        if customer_id:
            customer = get_object_or_404(
                Customer.objects.filter(organization=organization), pk=customer_id
            )

        rows = [
            {
                "id": job.pk,
                "customer": job.customer_id,
                "customer_name": job.customer.display_name,
                "service_name": job.service.name,
                "description": services.line_description(job),
                "scheduled_start": job.scheduled_start,
                "status": job.status,
                "amount_cents": services.billable_amount_cents(job),
            }
            for job in services.billable_jobs(organization, customer=customer)
        ]

        return Response(BillableJobSerializer(rows, many=True).data)


class InvoiceLineViewSet(TenantViewSetMixin, ModelViewSet):
    """
    Adjustments on a draft.

    Visit and no-access lines are built by `draft_invoice` from the visit
    itself and are not writable here; a hand-typed visit line would be a number
    with no job behind it. Every write is refused once the invoice is issued.
    """

    queryset = InvoiceLine.objects.select_related("invoice", "job", "organization")
    serializer_class = InvoiceLineSerializer
    permission_classes = [IsDispatcherOrHigher]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["invoice", "kind"]
    ordering_fields = ["position", "created_at"]

    def perform_update(self, serializer):
        services.assert_draft(serializer.instance.invoice, "edit a line on")
        super().perform_update(serializer)

    def perform_destroy(self, instance):
        services.assert_draft(instance.invoice, "remove a line from")
        instance.delete()

    @extend_schema(
        request=None,
        responses={200: InvoiceLineSerializer, 409: DetailSerializer},
        summary="Re-price an hourly visit from the hours actually worked",
        description=(
            "Never automatic: a crew running long is not self-evidently the "
            "customer's bill, so a person decides (ADR-026). Open time entries "
            "are ignored, and the line floors at the service's minimum charge."
        ),
    )
    @action(detail=True, methods=["post"])
    def reprice(self, request, pk=None):
        line = services.reprice_line_from_time_worked(self.get_object())
        return Response(self.get_serializer(line).data)


class PaymentViewSet(TenantViewSetMixin, ModelViewSet):
    """
    The ledger. Write once, then void with a reason -- no PATCH, no DELETE
    (ADR-025).
    """

    queryset = Payment.objects.select_related("invoice", "recorded_by", "organization")
    serializer_class = PaymentSerializer
    permission_classes = [IsDispatcherOrHigher]
    http_method_names = ["get", "post", "head", "options"]

    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_class = PaymentFilterSet
    ordering_fields = ["received_on", "created_at", "amount_cents"]
    ordering = ["-received_on"]

    def get_serializer_class(self):
        if self.action == "create":
            return RecordPaymentSerializer
        return PaymentSerializer

    @extend_schema(
        request=RecordPaymentSerializer,
        responses={201: PaymentSerializer, 400: DetailSerializer, 409: DetailSerializer},
        summary="Record money received against an invoice",
    )
    def create(self, request, *args, **kwargs):
        body = RecordPaymentSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data

        invoice = get_object_or_404(
            Invoice.objects.filter(organization=self.get_organization()), pk=data["invoice"]
        )

        payment = services.record_payment(
            invoice,
            method=data["method"],
            amount_cents=data["amount_cents"],
            tip_cents=data.get("tip_cents", 0),
            received_on=data["received_on"],
            reference=data.get("reference", ""),
            actor=request.user,
        )

        return Response(
            PaymentSerializer(payment, context=self.get_serializer_context()).data, status=201
        )

    @extend_schema(
        request=ReasonSerializer,
        responses={200: PaymentSerializer, 400: DetailSerializer, 409: DetailSerializer},
        summary="Void a payment, giving the reason",
    )
    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        body = ReasonSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        payment = services.void_payment(
            self.get_object(), actor=request.user, reason=body.validated_data["reason"]
        )
        return Response(PaymentSerializer(payment, context=self.get_serializer_context()).data)
