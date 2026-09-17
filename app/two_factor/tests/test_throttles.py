"""
The sign-in throttles actually throttle.

The test settings raise every rate so the rest of the suite cannot trip one by
accident, which also meant nothing ever proved a 429 was possible. DRF copies
the rates onto the throttle class at import, so they are patched there rather
than through the `settings` fixture, which it would not notice.
"""

import pytest
from django.core.cache import cache
from django.urls import reverse
from rest_framework.throttling import SimpleRateThrottle

LOGIN_URL = reverse("two_factor:login")
MAGIC_LINK_URL = reverse("users:magic-link-request")


@pytest.fixture(autouse=True)
def _tight_rates(monkeypatch):
    monkeypatch.setattr(
        SimpleRateThrottle,
        "THROTTLE_RATES",
        {
            "two_factor_issue": "3/hour",
            "two_factor_verify": "3/hour",
            "magic_link": "3/hour",
            "login_account": "5/hour",
        },
    )
    cache.clear()
    yield
    cache.clear()


def _attempt(client, email="owner@example.com", **extra):
    return client.post(LOGIN_URL, {"email": email, "password": "wrong"}, format="json", **extra)


@pytest.mark.django_db
class TestLoginThrottle:
    def test_one_address_runs_out_of_attempts(self, api_client):
        statuses = [_attempt(api_client).status_code for _ in range(4)]

        assert statuses == [401, 401, 401, 429]

    def test_a_forged_forwarded_header_does_not_buy_a_fresh_bucket(self, api_client):
        """NUM_PROXIES is 0 here: nothing in front of us, so the header is noise."""
        statuses = [
            _attempt(api_client, HTTP_X_FORWARDED_FOR=f"10.0.0.{n}").status_code for n in range(4)
        ]

        assert statuses[-1] == 429

    def test_one_account_runs_out_of_attempts_whatever_the_address(self, api_client):
        statuses = [
            _attempt(api_client, REMOTE_ADDR=f"203.0.113.{n}").status_code for n in range(6)
        ]

        assert statuses == [401] * 5 + [429]

    def test_another_account_is_unaffected(self, api_client):
        for n in range(6):
            _attempt(api_client, REMOTE_ADDR=f"203.0.113.{n}")

        response = _attempt(
            api_client, email="someone-else@example.com", REMOTE_ADDR="198.51.100.1"
        )

        assert response.status_code == 401


@pytest.mark.django_db
class TestMagicLinkThrottle:
    def test_requests_are_limited(self, api_client):
        statuses = [
            api_client.post(MAGIC_LINK_URL, {"email": "a@example.com"}).status_code
            for _ in range(4)
        ]

        assert statuses == [202, 202, 202, 429]
