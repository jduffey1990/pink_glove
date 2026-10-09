from django.utils import timezone
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from organizations import services as coverage
from organizations.enums import BillingState, NoticeLevel, StripeState
from organizations.models import Organization, validate_timezone, validate_working_days
from users.enums import Role


def role_for_notice(context: dict) -> str | None:
    """
    Whose eyes the billing notice is written for (ADR-023).

    An explicit `role` in the context wins (the session payload passes each
    membership's own); otherwise the caller's membership in the resolved
    organization; a superuser reads as the owner, since they can do what
    the owner can; nobody known reads as "ask the owner".
    """
    role = context.get("role")
    if role:
        return role
    request = context.get("request")
    membership = getattr(request, "membership", None)
    if membership is not None:
        return membership.role
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_superuser", False):
        return Role.OWNER
    return None


class BillingNoticeSerializer(serializers.Serializer):
    """
    The three fields the shell draws the banner from (Phase 4c, ADR-028):
    the server's one word for the state, how loudly to say it, and the
    sentence itself -- written for the reader's role, never composed on the
    client (ADR-023). On the session's organization summary, so the banner
    needs no second request.
    """

    state = serializers.ChoiceField(choices=BillingState.choices)
    level = serializers.ChoiceField(choices=NoticeLevel.choices, allow_null=True)
    notice = serializers.CharField(allow_null=True)

    def to_representation(self, organization):
        now = timezone.now()
        notice = coverage.billing_notice(organization, role_for_notice(self.context), now)
        return {
            "state": coverage.billing_state(organization, now),
            "level": notice[0] if notice else None,
            "notice": notice[1] if notice else None,
        }


class PlatformBillingSerializer(BillingNoticeSerializer):
    """
    The whole of the organization's subscription state, for the settings
    card. Read-only by construction: the fields it reads are written only by
    `billing.subscription`, the platform webhook and the admin.
    `available_actions` is what the subscription endpoints will accept, so
    the page draws its buttons from it and not from `status`.
    """

    summary = serializers.CharField(
        help_text="One sentence for the settings card, always present; the notice when there is one."
    )
    trial_ends_at = serializers.DateTimeField(allow_null=True)
    covered_until = serializers.DateTimeField(allow_null=True)
    grace_ends_at = serializers.DateTimeField(allow_null=True)
    status = serializers.CharField(help_text="Stripe's own word, or empty with no subscription.")
    cancel_at_period_end = serializers.BooleanField()
    exempt = serializers.BooleanField()
    available_actions = serializers.ListField(child=serializers.CharField())

    def to_representation(self, organization):
        role = role_for_notice(self.context)
        if not coverage.is_staff(role):
            # The tenant's customers: the state word (they can see read-only
            # for themselves) and none of the commercial relationship.
            return {
                **super().to_representation(organization),
                "summary": "",
                "trial_ends_at": None,
                "covered_until": None,
                "grace_ends_at": None,
                "status": "",
                "cancel_at_period_end": False,
                "exempt": False,
                "available_actions": [],
            }

        stamp = self.fields["trial_ends_at"].to_representation

        def when(value):
            return stamp(value) if value is not None else None

        return {
            **super().to_representation(organization),
            "summary": coverage.billing_summary(organization, role),
            "trial_ends_at": when(organization.trial_ends_at),
            "covered_until": when(coverage.covered_until(organization)),
            "grace_ends_at": when(coverage.grace_ends_at(organization)),
            "status": organization.subscription_status,
            "cancel_at_period_end": organization.subscription_cancel_at_period_end,
            "exempt": organization.billing_exempt,
            "available_actions": coverage.available_actions(organization),
        }


class StripeStatusSerializer(serializers.Serializer):
    """
    The organization's Stripe Connect state, as one thing to look at.

    Read-only by construction: the fields it reads are written only by
    `billing.connect` and the webhook. `state` is the server's one-word
    answer the settings page switches on (ADR-023); the flags are there for
    the copy. `connected` is "there is an account"; `charges_enabled` is
    "Stripe will take a card" -- the pay link waits for the second.
    """

    state = serializers.ChoiceField(choices=StripeState.choices, source="stripe_state")
    connected = serializers.BooleanField(source="stripe_connected")
    charges_enabled = serializers.BooleanField(source="stripe_charges_enabled")
    details_submitted = serializers.BooleanField(source="stripe_details_submitted")
    connected_at = serializers.DateTimeField(source="stripe_connected_at", allow_null=True)


class OrganizationSerializer(serializers.ModelSerializer):
    """
    The organization as its own admins see it.

    Writes are gated to admin+ by `CurrentOrganizationView.get_permissions`,
    so the operating-window fields need no further permission handling here.
    """

    stripe = serializers.SerializerMethodField()
    billing = serializers.SerializerMethodField()

    timezone = serializers.CharField(validators=[validate_timezone])
    working_days = serializers.ListField(
        child=serializers.IntegerField(min_value=1, max_value=7),
        validators=[validate_working_days],
        help_text="ISO weekday numbers: 1 = Monday .. 7 = Sunday.",
        required=False,
    )

    class Meta:
        model = Organization
        fields = (
            "id",
            "name",
            "slug",
            "timezone",
            "email",
            "phone",
            "address_line1",
            "address_line2",
            "address_city",
            "address_state",
            "address_postal_code",
            "address_country",
            "logo",
            "primary_color",
            "business_hours_start",
            "business_hours_end",
            "working_days",
            "reveal_buffer_before_minutes",
            "reveal_buffer_after_minutes",
            "tax_rate_percent",
            "no_access_fee_type",
            "no_access_fee_value",
            "invoice_prefix",
            "invoice_terms_days",
            "invoice_footer",
            "stripe",
            "billing",
            "is_active",
            "created_at",
        )
        read_only_fields = ("id", "slug", "is_active", "created_at")

    @extend_schema_field(StripeStatusSerializer)
    def get_stripe(self, organization: Organization) -> dict:
        return StripeStatusSerializer(organization).data

    @extend_schema_field(PlatformBillingSerializer)
    def get_billing(self, organization: Organization) -> dict:
        return PlatformBillingSerializer(organization, context=self.context).data
