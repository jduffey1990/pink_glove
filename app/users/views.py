import logging

from django.conf import settings
from django.contrib.auth import login, logout
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import ensure_csrf_cookie
from django_filters import rest_framework as filters
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import mixins, status
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework.viewsets import GenericViewSet

from base.permissions import IsAdminOrHigher, IsStaff
from base.serializers import DetailSerializer
from base.viewsets import TenantViewSetMixin
from users import services
from users.enums import DISPATCHER_ROLES, STAFF_ROLES
from users.models import CustomUser, MagicLinkToken, Membership
from users.serializers import (
    InvitationAcceptSerializer,
    InvitationInspectSerializer,
    InvitationSerializer,
    InvitedTeamMemberSerializer,
    InviteOptionsSerializer,
    InviteSerializer,
    MagicLinkConsumeSerializer,
    MagicLinkRequestSerializer,
    MagicLinkSentSerializer,
    MembershipRoleSerializer,
    SessionSerializer,
    TeamMemberSerializer,
)
from users.services import may_use_magic_link, send_magic_link

logger = logging.getLogger(__name__)


class SessionView(APIView):
    """Who am I, and which organization am I acting in?"""

    permission_classes = [AllowAny]

    @extend_schema(
        responses={200: SessionSerializer},
        summary="Current session",
        description=(
            "Returns an empty object when nobody is signed in. Always sets the "
            "csrftoken cookie, which is what the SPA needs before its first POST."
        ),
    )
    # This GET is the SPA's first call on boot, and nothing else sets the
    # csrftoken cookie before it. Without the cookie a fresh browser's very
    # first POST -- the login -- fails CSRF.
    @method_decorator(ensure_csrf_cookie)
    def get(self, request):
        if not request.user.is_authenticated:
            return Response({}, status=status.HTTP_200_OK)
        return Response(SessionSerializer(request.user, context={"request": request}).data)


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=None, responses={200: DetailSerializer}, summary="Sign out")
    def post(self, request):
        logout(request)
        return Response({"detail": "Signed out."})


class MagicLinkRequestView(APIView):
    """
    Passwordless sign-in for customers.

    Always returns 202, whether or not the address matches an account -- a
    different response would turn this into an account-enumeration oracle.
    """

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "magic_link"

    @extend_schema(
        request=MagicLinkRequestSerializer,
        responses={202: MagicLinkSentSerializer},
        summary="Request a sign-in link",
    )
    def post(self, request):
        serializer = MagicLinkRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"].lower()

        user = CustomUser.objects.filter(email=email, is_active=True).first()
        body = {"detail": "If that address has an account, a sign-in link is on its way."}

        # Staff get the same 202 as a stranger: they sign in with a password
        # and a code, and saying so here would reveal which addresses are staff.
        if user is None or not may_use_magic_link(user):
            return Response(body, status=status.HTTP_202_ACCEPTED)

        token, raw_token = MagicLinkToken.issue(user)
        link = f"{settings.FRONTEND_BASE_URL}/sign-in/{raw_token}"

        if settings.LOCAL:
            body["dev_link"] = link
        else:
            send_magic_link(user, link)

        return Response(body, status=status.HTTP_202_ACCEPTED)


class MagicLinkConsumeView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "magic_link"

    @extend_schema(
        request=MagicLinkConsumeSerializer,
        responses={200: SessionSerializer, 401: DetailSerializer},
        summary="Exchange a sign-in link for a session",
    )
    def post(self, request):
        serializer = MagicLinkConsumeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = MagicLinkToken.consume(serializer.validated_data["token"])
        # Asked again here because a role can change while a link is live.
        if user is None or not user.is_active or not may_use_magic_link(user):
            return Response(
                {"detail": "That sign-in link is invalid or has expired."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        login(request, user)
        return Response(SessionSerializer(user, context={"request": request}).data)


class MembershipFilterSet(filters.FilterSet):
    #: The team page's list: everyone who works here, none of the customers.
    staff = filters.BooleanFilter(method="filter_staff")

    class Meta:
        model = Membership
        fields = ["staff"]

    def filter_staff(self, queryset, name, value):
        if value:
            return queryset.filter(role__in=STAFF_ROLES)
        return queryset.exclude(role__in=STAFF_ROLES)


#: The actions that send an invitation email, and so share one throttle.
_INVITING_ACTIONS = ("create", "resend_invite", "reactivate")

_REFUSALS = {403: DetailSerializer, 409: DetailSerializer}


@extend_schema_view(
    list=extend_schema(summary="Who belongs to this organization"),
    create=extend_schema(
        request=InviteSerializer,
        responses={201: InvitedTeamMemberSerializer, **_REFUSALS},
        summary="Invite someone to the team",
    ),
    partial_update=extend_schema(
        request=MembershipRoleSerializer,
        responses={200: TeamMemberSerializer, **_REFUSALS},
        summary="Change someone's role",
    ),
)
class MembershipViewSet(
    TenantViewSetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    GenericViewSet,
):
    """
    Who belongs to the current organization, and the team page's writes.

    Reading is the staff directory it has been since Phase 1. Writing is for
    admins and owners, and the role tier is only the first gate: who may be
    managed by whom is decided per row in `users.services`, which is also
    where each row's `available_actions` come from (ADR-028).

    There is no PUT and no DELETE. A PATCH changes the role and nothing else;
    switching someone off or on is an action with its own rules, and a
    membership is never deleted from here.
    """

    queryset = Membership.objects.select_related("user", "organization").order_by("created_at")
    serializer_class = TeamMemberSerializer
    filterset_class = MembershipFilterSet
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsStaff()]
        return [IsAdminOrHigher()]

    def get_throttles(self):
        # Each of these puts an email in a mailbox the caller names. Keyed on
        # the signed-in user, so one account cannot be used to flood anyone.
        if self.action in _INVITING_ACTIONS:
            self.throttle_scope = "team_invite"
            return [ScopedRateThrottle()]
        return super().get_throttles()

    def _row(self, membership, link=None, status_code=status.HTTP_200_OK):
        """The row as the caller now sees it, re-judged after the write."""
        body = TeamMemberSerializer(membership, context=self.get_serializer_context()).data
        if link and settings.LOCAL:
            # Local only, as with `dev_code`: no mail server to read it from.
            body["dev_link"] = link
        return Response(body, status=status_code)

    def create(self, request, *args, **kwargs):
        serializer = InviteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # The organization is the resolved tenant, never a field of the body.
        membership, link = services.invite(
            services.Actor.of(request),
            self.get_organization(),
            serializer.validated_data["email"],
            serializer.validated_data["role"],
        )
        return self._row(membership, link, status.HTTP_201_CREATED)

    def partial_update(self, request, *args, **kwargs):
        membership = self.get_object()
        serializer = MembershipRoleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        membership = services.change_role(
            services.Actor.of(request), membership, serializer.validated_data["role"]
        )
        return self._row(membership)

    @extend_schema(
        request=None,
        responses={200: InviteOptionsSerializer},
        summary="The roles the caller may invite someone as",
    )
    @action(detail=False, methods=["get"], url_path="invite-options")
    def invite_options(self, request):
        """
        Served rather than derived on the page from the caller's own role:
        which tiers an admin may hand out is the server's rule (ADR-023).
        """
        return Response({"roles": services.invitable_roles(services.Actor.of(request))})

    @extend_schema(
        request=None,
        responses={200: TeamMemberSerializer, **_REFUSALS},
        summary="Switch someone's membership off",
    )
    @action(detail=True, methods=["post"])
    def deactivate(self, request, pk=None):
        membership = services.deactivate(services.Actor.of(request), self.get_object())
        return self._row(membership)

    @extend_schema(
        request=None,
        responses={200: InvitedTeamMemberSerializer, **_REFUSALS},
        summary="Switch someone's membership back on",
        description=(
            "Someone who had accepted is active again at once. A withdrawn "
            "invitation becomes an invitation again and a fresh link is sent."
        ),
    )
    @action(detail=True, methods=["post"])
    def reactivate(self, request, pk=None):
        membership, link = services.reactivate(services.Actor.of(request), self.get_object())
        return self._row(membership, link)

    @extend_schema(
        request=None,
        responses={200: InvitedTeamMemberSerializer, **_REFUSALS},
        summary="Send the invitation again",
        description="The link in the earlier email stops working.",
    )
    @action(detail=True, methods=["post"], url_path="resend-invite")
    def resend_invite(self, request, pk=None):
        membership, link = services.resend_invite(services.Actor.of(request), self.get_object())
        return self._row(membership, link)

    def get_queryset(self):
        """
        A staff directory for cleaners; the whole membership list for the
        office. A cleaner meets the customers they need on the job itself,
        with the contact details that job warrants -- not as a list of every
        homeowner's email address.
        """
        queryset = super().get_queryset()
        if self.request.user.is_superuser:
            return queryset

        membership = getattr(self.request, "membership", None)
        if membership is not None and membership.role in DISPATCHER_ROLES:
            return queryset
        return queryset.filter(role__in=STAFF_ROLES)


class InvitationInspectView(APIView):
    """
    What an invitation link opens, so the page can say which organization is
    asking and whether a password is needed.

    Public: the token is the credential. A POST with the token in the body,
    like the magic link, so it is never part of a URL this API logs.
    """

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "invite_accept"

    @extend_schema(
        request=InvitationInspectSerializer,
        responses={200: InvitationSerializer, 400: DetailSerializer},
        summary="Look at an invitation",
    )
    def post(self, request):
        serializer = InvitationInspectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        token = services.find_invitation(serializer.validated_data["token"])
        if token is None:
            return Response(
                {"detail": services.INVALID_INVITATION}, status=status.HTTP_400_BAD_REQUEST
            )

        user, membership = token.user, token.membership
        return Response(
            {
                "organization_name": membership.organization.name,
                "email": user.email,
                "role": membership.role,
                "must_set_password": services.must_set_password(user),
                "first_name": user.first_name,
                "last_name": user.last_name,
            }
        )


class InvitationAcceptView(APIView):
    """
    Accept an invitation. Public, and it signs nobody in: the response is a
    sentence, and the person then signs in the ordinary way and meets the
    ordinary challenge (ADR-008).
    """

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "invite_accept"

    @extend_schema(
        request=InvitationAcceptSerializer,
        responses={200: DetailSerializer, 400: DetailSerializer, 409: DetailSerializer},
        summary="Accept an invitation",
    )
    def post(self, request):
        serializer = InvitationAcceptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        membership = services.accept_invitation(
            data["token"],
            password=data.get("password", ""),
            first_name=data.get("first_name", ""),
            last_name=data.get("last_name", ""),
        )
        return Response(
            {"detail": f"You have joined {membership.organization.name}. Sign in to get started."}
        )
