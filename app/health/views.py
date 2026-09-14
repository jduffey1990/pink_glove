"""
Health endpoints, split because orchestrators probe them differently.

/health/live/   - the process is up. Touches nothing. A failure here should
                  restart the container.
/health/ready/  - dependencies are reachable. A failure here should pull the
                  instance out of the load balancer but NOT restart it.

Collapsing these into one endpoint means a brief database blip restart-loops
every container at once.
"""

import logging

from django.db import connection
from django.http import JsonResponse
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from health.serializers import LivenessSerializer, ReadinessSerializer

logger = logging.getLogger(__name__)


class LivenessView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(responses={200: LivenessSerializer}, summary="Liveness probe")
    def get(self, request):
        return JsonResponse({"status": "ok"})


class ReadinessView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        responses={200: ReadinessSerializer, 503: ReadinessSerializer},
        summary="Readiness probe",
    )
    def get(self, request):
        checks = {"database": self._check_database(), "redis": self._check_redis()}
        healthy = all(result == "ok" for result in checks.values())

        return JsonResponse(
            {"status": "ok" if healthy else "degraded", "checks": checks},
            status=200 if healthy else 503,
        )

    @staticmethod
    def _check_database() -> str:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
            return "ok"
        except Exception as exc:
            logger.warning("Readiness: database check failed: %s", exc)
            return f"error: {exc}"

    @staticmethod
    def _check_redis() -> str:
        try:
            import redis
            from django.conf import settings

            client = redis.from_url(settings.REDIS_URL, socket_connect_timeout=2)
            client.ping()
            return "ok"
        except Exception as exc:
            logger.warning("Readiness: redis check failed: %s", exc)
            return f"error: {exc}"
