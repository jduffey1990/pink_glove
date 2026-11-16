"""
What `sentry_probe.py` breaks on purpose: a view that raises, a task that
raises, and the values planted where real credentials would be. It is the
probe's URLconf, imported only in that child process. Not collected by pytest.
"""

import logging

from celery import shared_task
from django.urls import path
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from app.tests.sentry_probe import PLANTED

logger = logging.getLogger("app.tests.sentry_probe")


class BoomView(APIView):
    """Reads a location's codes and then fails, as a real view could."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request, token):
        gate_code = request.data["gate_code"]
        # A local whose name gives nothing away, holding a different secret.
        value = PLANTED["local"]
        logger.info("probe request for %s", request.get_full_path())
        raise RuntimeError(f"probe view failed ({len(gate_code)}, {len(value)})")


@shared_task(name="probe.boom")
def boom_task(organization_id: str, gate_code: str = "") -> None:
    raise RuntimeError("probe task failed")


urlpatterns = [path("probe/<str:token>/boom/", BoomView.as_view())]
