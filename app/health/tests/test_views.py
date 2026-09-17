from unittest import mock

import pytest
from django.urls import reverse


class TestLiveness:
    def test_returns_ok_without_touching_the_database(self, api_client):
        # No django_db marker on purpose: liveness must not need the DB.
        response = api_client.get(reverse("health:live"))

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_is_public(self, api_client):
        assert api_client.get(reverse("health:live")).status_code == 200


@pytest.mark.django_db
class TestReadiness:
    def test_reports_ok_when_dependencies_are_reachable(self, api_client):
        with mock.patch("health.views.ReadinessView._check_redis", return_value="ok"):
            response = api_client.get(reverse("health:ready"))

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["checks"]["database"] == "ok"

    def test_returns_503_when_a_dependency_is_down(self, api_client):
        with mock.patch("health.views.ReadinessView._check_redis", return_value="error"):
            response = api_client.get(reverse("health:ready"))

        assert response.status_code == 503
        assert response.json()["status"] == "degraded"

    def test_a_failure_does_not_describe_the_infrastructure(self, api_client):
        """Public endpoint; a driver error names the host, port, database and user."""
        with mock.patch(
            "redis.from_url", side_effect=ConnectionError("redis://secret-host:6379 refused")
        ):
            response = api_client.get(reverse("health:ready"))

        assert response.status_code == 503
        assert response.json()["checks"]["redis"] == "error"
        assert "secret-host" not in response.content.decode()
