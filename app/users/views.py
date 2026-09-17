import logging

from django.conf import settings
from django.contrib.auth import login, logout
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import ensure_csrf_cookie
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework.viewsets import ReadOnlyModelViewSet

from base.permissions import IsStaff
from base.serializers import DetailSerializer
from base.viewsets import TenantViewSetMixin
from users.enums import DISPATCHER_ROLES, STAFF_ROLES
from users.models import CustomUser, MagicLinkToken, Membership
from users.serializers import (
    MagicLinkConsumeSerializer,
    MagicLinkRequestSerializer,
    MagicLinkSentSerializer,
    MembershipSerializer,
    SessionSerializer,
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


class MembershipViewSet(TenantViewSetMixin, ReadOnlyModelViewSet):
    """Who belongs to the current organization. Read-only in Phase 1."""

    queryset = Membership.objects.select_related("user", "organization")
    serializer_class = MembershipSerializer
    permission_classes = [IsStaff]

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
