"""
Every Referer this site causes carries the origin and nothing more (ADR-038).

The header on the SPA's document and on whitenoise's files is tested in
test_spa.py. Here: the API's responses carry it, and the CSRF check still
passes for the Referer a browser now sends. Over https, CsrfViewMiddleware
reads `Origin` when there is one and otherwise insists on a `Referer` whose
scheme and host match; it never looks at the path, so `https://host/` is as
good as the full address was. A browser always sends `Origin` on a POST, so
the Referer branch is for clients that leave it out.
"""

import pytest
from django.conf import settings
from django.urls import reverse
from rest_framework.test import APIClient

ORIGIN = "https://testserver"


def test_every_environment_sends_strict_origin():
    assert settings.SECURE_REFERRER_POLICY == "strict-origin"


@pytest.mark.django_db
def test_an_api_response_carries_the_policy(api_client):
    response = api_client.get(reverse("users:session"))

    assert response["Referrer-Policy"] == "strict-origin"


@pytest.mark.django_db
class TestCsrfWithAnOriginOnlyReferer:
    """A signed-in POST, because DRF checks CSRF only for a session user."""

    @pytest.fixture
    def client(self, owner):
        client = APIClient(enforce_csrf_checks=True)
        client.force_login(owner)
        client.get(reverse("users:session"), secure=True)
        return client

    def _logout(self, client, **headers):
        token = client.cookies[settings.CSRF_COOKIE_NAME].value
        return client.post(reverse("users:logout"), secure=True, HTTP_X_CSRFTOKEN=token, **headers)

    def test_passes_with_no_origin(self, client):
        response = self._logout(client, HTTP_REFERER=f"{ORIGIN}/")

        assert response.status_code == 200

    def test_passes_as_a_browser_sends_it(self, client):
        response = self._logout(client, HTTP_ORIGIN=ORIGIN, HTTP_REFERER=f"{ORIGIN}/")

        assert response.status_code == 200

    def test_a_foreign_origin_fails(self, client):
        response = self._logout(client, HTTP_REFERER="https://evil.example/")

        assert response.status_code == 403
        assert "Referer checking failed" in response.json()["detail"]

    def test_no_referer_and_no_origin_fails(self, client):
        """The control: the Referer branch above is the one being exercised."""
        response = self._logout(client)

        assert response.status_code == 403
        assert "no Referer" in response.json()["detail"]
