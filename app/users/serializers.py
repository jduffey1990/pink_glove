from rest_framework import serializers

from organizations.models import Organization
from users.models import CustomUser, Membership


class OrganizationSummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = ("id", "name", "slug", "timezone", "primary_color", "logo")
        read_only_fields = fields


class MembershipSerializer(serializers.ModelSerializer):
    organization = OrganizationSummarySerializer(read_only=True)

    class Meta:
        model = Membership
        fields = ("id", "organization", "role", "is_active")
        read_only_fields = fields


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

    def get_current_organization(self, obj):
        organization = getattr(self.context.get("request"), "organization", None)
        if organization is None:
            return None
        return OrganizationSummarySerializer(organization, context=self.context).data

    def get_current_role(self, obj):
        membership = getattr(self.context.get("request"), "membership", None)
        return membership.role if membership else None


class MagicLinkRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()


class MagicLinkConsumeSerializer(serializers.Serializer):
    token = serializers.CharField()
