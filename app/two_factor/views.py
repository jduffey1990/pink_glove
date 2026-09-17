"""
Sign-in and two-factor verification.

Flow:
    POST /api/auth/login/   credentials -> either a session, or a pending
                            challenge recorded in the session
    POST /api/auth/verify/  code        -> session, optionally trusting the device
    POST /api/auth/resend/               -> a fresh code for the pending challenge

The pending challenge id lives in the session rather than in the response, so a
client cannot verify a challenge it was not issued.
"""

import logging

from django.conf import settings
from django.contrib.auth import authenticate, login
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from app.admin import TWO_FACTOR_VERIFIED_SESSION_KEY
from base.serializers import DetailSerializer
from base.throttles import LoginAccountThrottle
from two_factor.models import TrustedDevice, TwoFactorCode
from two_factor.serializers import (
    ChallengeIssuedSerializer,
    LoginSerializer,
    VerifySerializer,
)
from two_factor.services import TRUSTED_DEVICE_COOKIE, send_challenge, two_factor_required
from users.serializers import SessionSerializer

logger = logging.getLogger(__name__)

PENDING_CHALLENGE_SESSION_KEY = "pending_two_factor_id"

#: Deliberately identical for "no such user" and "wrong password", so the
#: endpoint cannot be used to enumerate accounts.
INVALID_CREDENTIALS = "Email and password do not match an active account."


def _set_device_cookie(response, raw_token: str) -> None:
    response.set_cookie(
        TRUSTED_DEVICE_COOKIE,
        raw_token,
        max_age=int(TrustedDevice.TTL.total_seconds()),
        secure=settings.SESSION_COOKIE_SECURE,
        httponly=True,
        samesite=settings.SESSION_COOKIE_SAMESITE,
        domain=settings.SESSION_COOKIE_DOMAIN,
    )


@extend_schema(
    request=LoginSerializer,
    responses={
        200: SessionSerializer,
        202: ChallengeIssuedSerializer,
        400: DetailSerializer,
        401: DetailSerializer,
    },
    summary="Sign in",
    description=(
        "200 means the session is established (no 2FA required, or a trusted "
        "device). 202 means a code was sent and POST /api/auth/verify/ is next."
    ),
)
class LoginView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle, LoginAccountThrottle]
    throttle_scope = "two_factor_issue"

    def post(self, request):
        email = (request.data.get("email") or "").strip().lower()
        password = request.data.get("password") or ""

        if not email or not password:
            return Response(
                {"detail": "Email and password are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = authenticate(request, email=email, password=password)
        if user is None or not user.is_active:
            return Response({"detail": INVALID_CREDENTIALS}, status=status.HTTP_401_UNAUTHORIZED)

        device_token = request.COOKIES.get(TRUSTED_DEVICE_COOKIE)
        if not two_factor_required(user, device_token):
            login(request, user)
            return Response(SessionSerializer(user, context={"request": request}).data)

        challenge, raw_code = send_challenge(user)
        request.session[PENDING_CHALLENGE_SESSION_KEY] = str(challenge.pk)

        body = {"two_factor_required": True, "detail": "A sign-in code has been sent."}
        if settings.LOCAL:
            # Local only, so a developer with no mail server can still sign in.
            body["dev_code"] = raw_code

        return Response(body, status=status.HTTP_202_ACCEPTED)


@extend_schema(
    request=VerifySerializer,
    responses={200: SessionSerializer, 400: DetailSerializer, 401: DetailSerializer},
    summary="Submit a sign-in code",
)
class VerifyView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "two_factor_verify"

    def post(self, request):
        challenge = self._pending_challenge(request)
        if challenge is None:
            return Response(
                {"detail": "No sign-in is in progress. Please start again."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        submitted = (request.data.get("code") or "").strip()
        if not submitted:
            return Response({"detail": "A code is required."}, status=status.HTTP_400_BAD_REQUEST)

        if not challenge.verify(submitted):
            if not challenge.is_usable:
                del request.session[PENDING_CHALLENGE_SESSION_KEY]
                return Response(
                    {"detail": "That code has expired or been used too many times."},
                    status=status.HTTP_401_UNAUTHORIZED,
                )
            return Response(
                {"detail": "That code is not correct."}, status=status.HTTP_401_UNAUTHORIZED
            )

        user = challenge.user
        del request.session[PENDING_CHALLENGE_SESSION_KEY]
        login(request, user)  # cycles the session key
        # After login(), which may flush: this is what the admin site checks.
        request.session[TWO_FACTOR_VERIFIED_SESSION_KEY] = True

        response = Response(SessionSerializer(user, context={"request": request}).data)

        if request.data.get("remember_device"):
            _, raw_token = TrustedDevice.issue(
                user, user_agent=request.headers.get("User-Agent", "")
            )
            _set_device_cookie(response, raw_token)

        return response

    @staticmethod
    def _pending_challenge(request) -> TwoFactorCode | None:
        challenge_id = request.session.get(PENDING_CHALLENGE_SESSION_KEY)
        if not challenge_id:
            return None
        return TwoFactorCode.objects.filter(pk=challenge_id).first()


@extend_schema(
    request=None,
    responses={202: ChallengeIssuedSerializer, 400: DetailSerializer},
    summary="Resend the pending sign-in code",
)
class ResendView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "two_factor_issue"

    def post(self, request):
        challenge = VerifyView._pending_challenge(request)
        if challenge is None:
            return Response(
                {"detail": "No sign-in is in progress. Please start again."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        new_challenge, raw_code = send_challenge(challenge.user, resent=True)
        request.session[PENDING_CHALLENGE_SESSION_KEY] = str(new_challenge.pk)

        body = {"detail": "A new sign-in code has been sent."}
        if settings.LOCAL:
            body["dev_code"] = raw_code

        return Response(body, status=status.HTTP_202_ACCEPTED)
