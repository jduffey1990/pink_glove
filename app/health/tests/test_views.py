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

    def test_the_redis_probe_cannot_hang_and_leaves_no_connection_behind(self, api_client):
        """
        A connection Redis drops silently blocks a read with no timeout until
        TCP gives up, and gunicorn's gthread worker never reaps a hung
        request thread: four such probes and the web machine served nothing
        for half an hour (staging, 2026-09-23). Both timeouts, and the client
        closed, so a probe is bounded and leaves nothing open.
        """
        client = mock.Mock()
        with mock.patch("redis.from_url", return_value=client) as from_url:
            response = api_client.get(reverse("health:ready"))

        assert response.status_code == 200
        kwargs = from_url.call_args.kwargs
        assert kwargs["socket_connect_timeout"] == 2
        assert kwargs["socket_timeout"] == 2
        client.ping.assert_called_once()
        client.close.assert_called_once()

    def test_a_failure_does_not_describe_the_infrastructure(self, api_client):
        """Public endpoint; a driver error names the host, port, database and user."""
        with mock.patch(
            "redis.from_url", side_effect=ConnectionError("redis://secret-host:6379 refused")
        ):
            response = api_client.get(reverse("health:ready"))

        assert response.status_code == 503
        assert response.json()["checks"]["redis"] == "error"
        assert "secret-host" not in response.content.decode()


# What Fly's checker sends: the machine's own private address, which is
# different on every machine and so can never be in ALLOWED_HOSTS.
FLY_PROBE_HOST = "172.19.75.162:8000"


class TestProbesIgnoreTheHostHeader:
    """
    The first staging deploy hung on this: every probe answered 400, the
    check read critical, and the proxy never routed to a healthy machine.
    """

    @pytest.fixture(autouse=True)
    def strict_hosts(self, settings):
        settings.ALLOWED_HOSTS = ["testserver"]

    def test_liveness_answers_a_probe_from_an_unlisted_host(self, api_client):
        response = api_client.get(reverse("health:live"), HTTP_HOST=FLY_PROBE_HOST)

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    @pytest.mark.django_db
    def test_readiness_answers_a_probe_from_an_unlisted_host(self, api_client):
        with mock.patch("health.views.ReadinessView._check_redis", return_value="ok"):
            response = api_client.get(reverse("health:ready"), HTTP_HOST=FLY_PROBE_HOST)

        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    @pytest.mark.django_db
    def test_readiness_still_reports_a_failure_to_the_probe(self, api_client):
        """Short-circuiting the stack must not short-circuit the check itself."""
        with mock.patch("health.views.ReadinessView._check_redis", return_value="error"):
            response = api_client.get(reverse("health:ready"), HTTP_HOST=FLY_PROBE_HOST)

        assert response.status_code == 503

    def test_everything_else_still_refuses_an_unlisted_host(self, api_client):
        """The exemption is the two probes, not the host check."""
        for path in ("/", "/api/auth/session/", "/healthy/", "/health/"):
            response = api_client.get(path, HTTP_HOST=FLY_PROBE_HOST)
            assert response.status_code == 400, path

    def test_a_probe_from_a_listed_host_is_unchanged(self, api_client):
        assert api_client.get(reverse("health:live"), HTTP_HOST="testserver").status_code == 200
