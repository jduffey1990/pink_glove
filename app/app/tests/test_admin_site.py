"""
The admin site has no password door of its own (app/admin.py).

The thing being guarded against: `/admin/login/` used to accept an email and a
password and return the same session the API trusts -- no code, no throttle,
and for a superuser a session good in every tenant.
"""

import pytest
from django.urls import reverse

from two_factor.models import TwoFactorCode

ADMIN_INDEX = reverse("admin:index")
ADMIN_LOGIN = reverse("admin:login")
PASSWORD = "pw-for-tests-only"  # noqa: S105


@pytest.mark.django_db
class TestAdminNeedsTheSecondFactor:
    def test_a_password_posted_to_the_admin_login_signs_nobody_in(self, client, superuser):
        response = client.post(ADMIN_LOGIN, {"username": superuser.email, "password": PASSWORD})

        assert response.status_code == 302
        assert response["Location"].endswith("/login")
        assert "_auth_user_id" not in client.session

    def test_anonymous_is_sent_to_sign_in(self, client):
        response = client.get(ADMIN_INDEX, follow=False)

        assert response.status_code == 302
        assert ADMIN_LOGIN in response["Location"]

    def test_a_superuser_session_that_skipped_the_challenge_is_refused(self, client, superuser):
        """However it came by the session -- this is the door that was open."""
        client.force_login(superuser)

        assert client.get(ADMIN_INDEX).status_code == 302

    def test_a_superuser_who_cleared_the_challenge_gets_in(self, api_client, superuser, settings):
        settings.LOCAL = True
        login = api_client.post(
            reverse("two_factor:login"), {"email": superuser.email, "password": PASSWORD}
        )
        assert login.status_code == 202
        api_client.post(reverse("two_factor:verify"), {"code": login.json()["dev_code"]})

        assert api_client.get(ADMIN_INDEX).status_code == 200

    def test_clearing_the_challenge_does_not_make_an_owner_an_admin_user(
        self, api_client, owner, settings
    ):
        """The flag is necessary, not sufficient: is_staff still decides."""
        settings.LOCAL = True
        login = api_client.post(
            reverse("two_factor:login"), {"email": owner.email, "password": PASSWORD}
        )
        api_client.post(reverse("two_factor:verify"), {"code": login.json()["dev_code"]})

        assert TwoFactorCode.objects.filter(user=owner, consumed_at__isnull=False).exists()
        assert api_client.get(ADMIN_INDEX).status_code == 302
