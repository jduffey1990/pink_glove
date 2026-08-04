import logging

from django.conf import settings
from django.contrib.auth import login, logout
from django.core.mail import send_mail
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework.viewsets import ReadOnlyModelViewSet

from base.permissions import IsStaff
from base.viewsets import TenantViewSetMixin
from users.models import CustomUser, MagicLinkToken, Membership
from users.serializers import (
    MagicLinkConsumeSerializer,
    MagicLinkRequestSerializer,
    MembershipSerializer,
    SessionSerializer,
)

logger = logging.getLogger(__name__)


class SessionView(APIView):
    """Who am I, and which organization am I acting in?"""

    permission_classes = [AllowAny]

    def get(self, request):
        if not request.user.is_authenticated:
            return Response({}, status=status.HTTP_200_OK)
        return Response(SessionSerializer(request.user, context={"request": request}).data)


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

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

    def post(self, request):
        serializer = MagicLinkRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"].lower()

        user = CustomUser.objects.filter(email=email, is_active=True).first()
        body = {"detail": "If that address has an account, a sign-in link is on its way."}

        if user is None:
            return Response(body, status=status.HTTP_202_ACCEPTED)

        token, raw_token = MagicLinkToken.issue(user)
        link = f"{settings.FRONTEND_BASE_URL}/sign-in/{raw_token}"

        if settings.LOCAL:
            body["dev_link"] = link
        else:
            try:
                send_mail(
                    subject="Your sign-in link",
                    message=f"Sign in here: {link}\n\nThis link expires in 15 minutes.",
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[user.email],
                    fail_silently=False,
                )
            except Exception:
                logger.exception("Failed to send magic link to user %s", user.pk)

        return Response(body, status=status.HTTP_202_ACCEPTED)


class MagicLinkConsumeView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "magic_link"

    def post(self, request):
        serializer = MagicLinkConsumeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = MagicLinkToken.consume(serializer.validated_data["token"])
        if user is None or not user.is_active:
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
