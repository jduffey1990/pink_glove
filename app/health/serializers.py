"""
Response shapes for the health probes.

These endpoints answer to an orchestrator, not to the SPA, and they return
plain JsonResponses. The serializers exist so the generated schema describes
them rather than leaving two undocumented holes in it (ADR-019).
"""

from rest_framework import serializers


class LivenessSerializer(serializers.Serializer):
    status = serializers.CharField()


class ReadinessSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=["ok", "degraded"])
    checks = serializers.DictField(
        child=serializers.CharField(),
        help_text='Per-dependency result: "ok", or "error: <reason>".',
    )
