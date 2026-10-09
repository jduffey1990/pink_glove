from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from organizations.models import Organization
from organizations.serializers import BillingNoticeSerializer
from users.enums import Role
from users.models import CustomUser, Membership


class OrganizationSummarySerializer(serializers.ModelSerializer):
    """
    The organization as the shell sees it: enough to name it, reckon dates
    in its zone, and draw the billing banner (Phase 4c) without a second
    request. `is_active` is what the baseline gate owed the banner.
    """

    billing = serializers.SerializerMethodField()

    class Meta:
        model = Organization
        fields = ("id", "name", "slug", "timezone", "primary_color", "logo", "is_active", "billing")
        read_only_fields = fields

    @extend_schema_field(BillingNoticeSerializer)
    def get_billing(self, organization: Organization) -> dict:
        return BillingNoticeSerializer(organization, context=self.context).data


class MembershipSerializer(serializers.ModelSerializer):
    """
    Who belongs to this organization.

    The user fields are what make this usable as a staff directory: the
    frontend's assignee pickers need a *user* id to POST to the job assign
    action, and the membership id is not it. `JobAssignmentSerializer` already
    exposes the same name and email to the same audience (staff only), so this
    widens nothing.
    """

    organization = serializers.SerializerMethodField()
    user_name = serializers.CharField(source="user.full_name", read_only=True)
    user_email = serializers.EmailField(source="user.email", read_only=True)

    class Meta:
        model = Membership
        fields = ("id", "organization", "user", "user_name", "user_email", "role", "is_active")
        read_only_fields = fields

    @extend_schema_field(OrganizationSummarySerializer)
    def get_organization(self, membership: Membership) -> dict:
        # The billing notice is written for the caller: in their own role
        # when the row is their membership (the session's list), otherwise
        # in the role they hold in the organization being read (the staff
        # directory, where every row is the same organization).
        request = self.context.get("request")
        context = dict(self.context)
        if request is not None and membership.user_id == getattr(request.user, "id", None):
            context["role"] = membership.role
        return OrganizationSummarySerializer(membership.organization, context=context).data


class SessionSerializer(serializers.ModelSerializer):
    """
    What the frontend needs to render a signed-in shell.

    Includes every membership so a multi-organization user can be offered a
    switcher; the active one is echoed back as `current_organization`.
    """

    memberships = MembershipSerializer(many=True, read_only=True)
    current_organization = serializers.SerializerMethodField()
    current_role = serializers.SerializerMethodField()

    class Meta:
        model = CustomUser
        fields = (
            "id",
            "email",
            "first_name",
            "last_name",
            "full_name",
            "phone",
            "is_superuser",
            "memberships",
            "current_organization",
            "current_role",
        )
        read_only_fields = fields

    @extend_schema_field(OrganizationSummarySerializer(allow_null=True))
    def get_current_organization(self, obj):
        organization = getattr(self.context.get("request"), "organization", None)
        if organization is None:
            return None
        return OrganizationSummarySerializer(organization, context=self.context).data

    @extend_schema_field(serializers.ChoiceField(choices=Role.choices, allow_null=True))
    def get_current_role(self, obj):
        membership = getattr(self.context.get("request"), "membership", None)
        return membership.role if membership else None


class MagicLinkRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()


class MagicLinkConsumeSerializer(serializers.Serializer):
    token = serializers.CharField()


class MagicLinkSentSerializer(serializers.Serializer):
    """
    Always the same body whether or not the address matched an account -- a
    different one would make this an account-enumeration oracle.
    """

    detail = serializers.CharField()
    dev_link = serializers.CharField(
        required=False,
        help_text="Local development only. Never present in a deployed environment.",
    )
