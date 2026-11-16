from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from organizations.models import Organization
from organizations.serializers import BillingNoticeSerializer
from users import services
from users.enums import STAFF_ROLE_CHOICES, MembershipState, Role
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


class TeamMemberSerializer(MembershipSerializer):
    """
    A membership as the team page sees it: where it stands, and what the
    caller may do to it.

    `available_actions` and `assignable_roles` are worked out for whoever is
    asking, from the same rules the writes enforce (`users.services`), so the
    page draws its buttons from them and keeps no table of who outranks whom
    (ADR-023, ADR-028). The session payload keeps the plain
    `MembershipSerializer`: a person's own memberships have no caller to
    judge them by.
    """

    user_name = serializers.SerializerMethodField()
    state = serializers.ChoiceField(choices=MembershipState.choices, read_only=True)
    available_actions = serializers.SerializerMethodField()
    assignable_roles = serializers.SerializerMethodField()

    class Meta(MembershipSerializer.Meta):
        fields = (
            *MembershipSerializer.Meta.fields,
            "state",
            "invited_at",
            "accepted_at",
            "deactivated_at",
            "available_actions",
            "assignable_roles",
        )
        read_only_fields = fields

    def _rules(self):
        """The caller and the organization's sole owner, once per response."""
        if "team_rules" not in self.context:
            request = self.context["request"]
            self.context["team_rules"] = (
                services.Actor.of(request),
                services.sole_active_owner_id(request.organization),
            )
        return self.context["team_rules"]

    @extend_schema_field(serializers.CharField())
    def get_user_name(self, obj):
        """
        The address, until the invitation is accepted.

        An invited address may already have an account somewhere else, and
        its name is that person's, not this organization's to be shown. Until
        they join, the row says only what the inviter typed.
        """
        if obj.awaiting_acceptance:
            return obj.user.email
        return obj.user.full_name

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_available_actions(self, obj):
        actor, sole_owner_id = self._rules()
        return services.available_actions(actor, obj, sole_owner_id)

    @extend_schema_field(
        serializers.ListField(child=serializers.ChoiceField(choices=STAFF_ROLE_CHOICES))
    )
    def get_assignable_roles(self, obj):
        actor, sole_owner_id = self._rules()
        return services.assignable_roles(actor, obj, sole_owner_id)


class InvitedTeamMemberSerializer(TeamMemberSerializer):
    """A team row, with the invitation link when the API is running locally."""

    dev_link = serializers.CharField(
        required=False,
        help_text="Local development only. Never present in a deployed environment.",
    )

    class Meta(TeamMemberSerializer.Meta):
        fields = (*TeamMemberSerializer.Meta.fields, "dev_link")
        read_only_fields = fields


class InviteSerializer(serializers.Serializer):
    """
    An invitation: an address and a role, and nothing else.

    No name: an invited address may already belong to someone, and this
    organization does not get to name them. They name themselves when they
    accept. `customer` is not a choice; the portal is not handed out here.
    """

    email = serializers.EmailField()
    role = serializers.ChoiceField(choices=STAFF_ROLE_CHOICES)


class MembershipRoleSerializer(serializers.Serializer):
    """
    The one thing a PATCH may change. `user`, `organization` and `is_active`
    are not fields here, so sending them does nothing: activation has its own
    actions, with their own rules. Nobody is made a customer here, so that is
    not a choice either.
    """

    role = serializers.ChoiceField(choices=STAFF_ROLE_CHOICES)


class InviteOptionsSerializer(serializers.Serializer):
    roles = serializers.ListField(child=serializers.ChoiceField(choices=STAFF_ROLE_CHOICES))


class InvitationInspectSerializer(serializers.Serializer):
    token = serializers.CharField()


class InvitationSerializer(serializers.Serializer):
    """What an invitation link opens, shown to whoever holds it."""

    organization_name = serializers.CharField()
    email = serializers.EmailField()
    role = serializers.ChoiceField(choices=Role.choices)
    must_set_password = serializers.BooleanField(
        help_text=(
            "True when this address has never had a password. The accept call "
            "then needs a first name and a password; otherwise it needs neither."
        )
    )
    first_name = serializers.CharField(allow_blank=True)
    last_name = serializers.CharField(allow_blank=True)


class InvitationAcceptSerializer(serializers.Serializer):
    token = serializers.CharField()
    password = serializers.CharField(
        required=False,
        allow_blank=True,
        write_only=True,
        trim_whitespace=False,
        style={"input_type": "password"},
    )
    first_name = serializers.CharField(required=False, allow_blank=True, max_length=150)
    last_name = serializers.CharField(required=False, allow_blank=True, max_length=150)


class SessionSerializer(serializers.ModelSerializer):
    """
    What the frontend needs to render a signed-in shell.

    Includes every membership so a multi-organization user can be offered a
    switcher; the active one is echoed back as `current_organization`.

    "Every membership" means every one the middleware would resolve: a
    deactivated membership is not an organization this person can act in, so
    it is not offered. Listing one made the app count it, show it in the
    switcher and send its id in a header the middleware then refused.
    """

    memberships = serializers.SerializerMethodField()
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

    @extend_schema_field(MembershipSerializer(many=True))
    def get_memberships(self, obj):
        # The same filter as TenantMiddleware, so the two cannot disagree
        # about which organizations this person holds.
        memberships = (
            obj.memberships.filter(is_active=True)
            .select_related("user", "organization")
            .order_by("created_at")
        )
        return MembershipSerializer(memberships, many=True, context=self.context).data

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
