"""
Invoice and payment filtering.

`issued_from` / `issued_to` and `received_from` / `received_to` are plain
dates, not instants: `issued_on` and `received_on` are already
organization-local dates on the row, so there is no timezone arithmetic to do
here. The job filters convert because they filter a UTC datetime column; these
do not, and conflating the two is how a date filter starts losing the last day
of the month.
"""

from django_filters import rest_framework as filters

from billing.enums import InvoiceStatus, PaymentMethod, PaymentState
from billing.models import Invoice, Payment
from billing.services import is_overdue, payment_state


class InvoiceFilterSet(filters.FilterSet):
    status = filters.MultipleChoiceFilter(choices=InvoiceStatus.choices)
    issued_from = filters.DateFilter(field_name="issued_on", lookup_expr="gte")
    issued_to = filters.DateFilter(field_name="issued_on", lookup_expr="lte")

    # Both of these are derived from the ledger rather than stored (ADR-025),
    # so they are applied in Python over the page rather than in SQL. The
    # alternative is a stored flag that a voided payment silently falsifies.
    payment_state = filters.ChoiceFilter(
        choices=PaymentState.choices, method="filter_payment_state"
    )
    overdue = filters.BooleanFilter(method="filter_overdue")

    class Meta:
        model = Invoice
        fields = ["customer", "status"]

    def filter_payment_state(self, queryset, name, value):
        matching = [
            invoice.pk
            for invoice in queryset.select_related("organization")
            if payment_state(invoice) == value
        ]
        return queryset.filter(pk__in=matching)

    def filter_overdue(self, queryset, name, value):
        if value is None:
            return queryset
        matching = [
            invoice.pk
            for invoice in queryset.select_related("organization")
            if is_overdue(invoice) is bool(value)
        ]
        return queryset.filter(pk__in=matching)


class PaymentFilterSet(filters.FilterSet):
    method = filters.MultipleChoiceFilter(choices=PaymentMethod.choices)
    received_from = filters.DateFilter(field_name="received_on", lookup_expr="gte")
    received_to = filters.DateFilter(field_name="received_on", lookup_expr="lte")
    voided = filters.BooleanFilter(field_name="voided_at", lookup_expr="isnull", exclude=True)

    class Meta:
        model = Payment
        fields = ["invoice", "method"]
