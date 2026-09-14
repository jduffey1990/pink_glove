"""
The session endpoint is what bootstraps CSRF for the SPA.

Without this, a fresh browser has no `csrftoken` cookie when it makes its first
POST -- which is the login -- and that POST fails CSRF for reasons that look
like a credentials problem.
"""

import pytest
from django.conf import settings
from django.urls import reverse


@pytest.mark.django_db
class TestSessionSetsCsrfCookie:
    def test_anonymous_get_sets_the_cookie(self, api_client):
        response = api_client.get(reverse("users:session"))

        assert response.status_code == 200
        assert settings.CSRF_COOKIE_NAME in response.cookies

    def test_signed_in_get_sets_the_cookie(self, authed_client):
        response = authed_client.get(reverse("users:session"))

        assert response.status_code == 200
        assert settings.CSRF_COOKIE_NAME in response.cookies

    def test_the_cookie_is_readable_by_the_spa(self, api_client):
        """The SPA has to read this one to echo it back as X-CSRFToken."""
        response = api_client.get(reverse("users:session"))

        assert not response.cookies[settings.CSRF_COOKIE_NAME]["httponly"]
