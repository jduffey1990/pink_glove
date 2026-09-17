from rest_framework import serializers

from base.viewsets import TenantModelSerializer
from catalog.models import Service


class ServiceSerializer(TenantModelSerializer):
    class Meta(TenantModelSerializer.Meta):
        model = Service
        fields = (
            "id",
            "organization",
            "name",
            "description",
            "pricing_model",
            "base_price_cents",
            "hourly_rate_cents",
            "per_sqft_rate_cents",
            "default_duration_minutes",
            "is_active",
            "created_at",
            "updated_at",
        )

    def validate(self, attrs):
        # Run the model's own pricing-model coherence rules; DRF does not call
        # full_clean() on its own.
        instance = Service(**{**self._instance_values(), **attrs})
        instance.clean()
        return attrs

    def _instance_values(self) -> dict:
        if self.instance is None:
            return {}
        return {
            field: getattr(self.instance, field)
            for field in (
                "pricing_model",
                "base_price_cents",
                "hourly_rate_cents",
                "per_sqft_rate_cents",
            )
        }


class QuoteSerializer(serializers.Serializer):
    """Inputs for `POST /api/catalog/services/{id}/quote/`."""

    square_feet = serializers.IntegerField(required=False, min_value=0)
    hours = serializers.DecimalField(required=False, max_digits=6, decimal_places=2, min_value=0)


class QuoteResultSerializer(serializers.Serializer):
    """The quote action's response, described for the schema (ADR-019)."""

    service = serializers.CharField()
    pricing_model = serializers.CharField()
    amount_cents = serializers.IntegerField()
