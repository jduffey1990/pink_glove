"""
Request and response shapes for the sign-in flow.

The views read `request.data` directly and are left that way -- the flow
branches on credentials, trusted-device cookies, and session state in ways a
serializer would not simplify. These describe those bodies for the OpenAPI
schema so the SPA (Phase 3b) is written against a real contract rather than
against the sequence in a docstring.
"""

from rest_framework import serializers

from base.serializers import DetailSerializer


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, style={"input_type": "password"})


class VerifySerializer(serializers.Serializer):
    code = serializers.CharField()
    remember_device = serializers.BooleanField(
        required=False,
        default=False,
        help_text="Skip the code on this device until the trusted-device cookie expires.",
    )


class ChallengeIssuedSerializer(DetailSerializer):
    """202 body: a code went out and the client should collect it."""

    two_factor_required = serializers.BooleanField(required=False, default=True)
    dev_code = serializers.CharField(
        required=False,
        help_text="Local development only. Never present in a deployed environment.",
    )
