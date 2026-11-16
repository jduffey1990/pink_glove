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
from base.throttles import LoginAccountThrottle, PasswordResetAccountThrottle
from two_factor import services, tasks
from two_factor.models import TrustedDevice, TwoFactorCode
from two_factor.serializers import (
    ChallengeIssuedSerializer,
    LoginSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    PasswordResetSentSerializer,
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

        # A pending sign-in records that a password was accepted, not which
        # one. A password reset withdraws every outstanding code
        # (`services.forget_devices`), and that has to end the sign-in they
        # belonged to as well: otherwise whoever started it with the old
        # password asks here for a fresh code and finishes without ever being
        # asked for the new one (ADR-028). A spent code with a sign-in still
        # pending behind it only ever means that, because a code spent by
        # verifying takes the pending sign-in with it. An expired code is not
        # spent, so the ordinary "send me another" is untouched.
        if challenge is not None and challenge.consumed_at is not None:
            del request.session[PENDING_CHALLENGE_SESSION_KEY]
            challenge = None

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


#: Said to everyone who asks, whoever they are.
RESET_REQUESTED = "If that address belongs to a staff account, a reset link is on its way."


class PasswordResetRequestView(APIView):
    """
    Ask for a password reset link. Staff only; customers use magic links.

    Always 202 with the same body, whether the address is staff, a customer
    or nobody at all, and it takes the same time too: the view does not look
    the account up. It hands the address to a worker, which decides and sends
    (`two_factor.tasks.send_password_reset`).

    Throttled twice: per client address, and per address asked about, so
    neither one machine nor many can bury a person in reset emails.
    """

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle, PasswordResetAccountThrottle]
    throttle_scope = "password_reset"

    @extend_schema(
        request=PasswordResetRequestSerializer,
        responses={202: PasswordResetSentSerializer},
        summary="Request a password reset link",
    )
    def post(self, request):
        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"].lower()

        body = {"detail": RESET_REQUESTED}

        if settings.LOCAL:
            # Local only, as with `dev_code`: decided in the request so the
            # link can come back in the response, and nothing is sent. The
            # body therefore differs by account here, and only here.
            issued = services.issue_password_reset(email)
            if issued is not None:
                body["dev_link"] = issued[1]
        else:
            tasks.send_password_reset.delay(email)

        return Response(body, status=status.HTTP_202_ACCEPTED)


class PasswordResetConfirmView(APIView):
    """
    Set a new password with a reset link. Public, and it signs nobody in.

    On success every open session, trusted device and pending sign-in code
    for the account is gone (`services.reset_password`), and the next
    sign-in is password and code like any other.
    """

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "password_reset_confirm"

    @extend_schema(
        request=PasswordResetConfirmSerializer,
        responses={200: DetailSerializer, 400: DetailSerializer},
        summary="Set a new password with a reset link",
    )
    def post(self, request):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        services.reset_password(
            serializer.validated_data["token"], serializer.validated_data["password"]
        )
        return Response({"detail": "Your password has been changed. Sign in with it now."})
