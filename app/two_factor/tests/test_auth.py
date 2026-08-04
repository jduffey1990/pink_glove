import pytest
from django.test import override_settings
from django.urls import reverse

from two_factor.models import TrustedDevice, TwoFactorCode
from two_factor.services import TRUSTED_DEVICE_COOKIE
from users.enums import Role

LOGIN_URL = reverse("two_factor:login")
VERIFY_URL = reverse("two_factor:verify")
RESEND_URL = reverse("two_factor:resend")
PASSWORD = "pw-for-tests-only"


def _login(client, email, password=PASSWORD, **extra):
    return client.post(LOGIN_URL, {"email": email, "password": password}, **extra)


@pytest.mark.django_db
class TestLogin:
    def test_wrong_password_is_rejected(self, api_client, owner):
        response = _login(api_client, owner.email, "wrong-password")

        assert response.status_code == 401

    def test_unknown_email_gives_the_same_message_as_a_wrong_password(self, api_client, owner):
        """Otherwise the endpoint is an account-enumeration oracle."""
        unknown = _login(api_client, "nobody@example.com")
        wrong = _login(api_client, owner.email, "wrong-password")

        assert unknown.status_code == wrong.status_code == 401
        assert unknown.json()["detail"] == wrong.json()["detail"]

    def test_missing_fields_are_a_400(self, api_client):
        assert api_client.post(LOGIN_URL, {"email": "a@b.com"}).status_code == 400

    def test_owner_is_always_challenged(self, api_client, owner):
        response = _login(api_client, owner.email)

        assert response.status_code == 202
        assert response.json()["two_factor_required"] is True
        assert TwoFactorCode.objects.filter(user=owner).count() == 1

    def test_challenge_does_not_sign_the_user_in(self, api_client, owner):
        _login(api_client, owner.email)

        assert api_client.get(reverse("users:session")).json() == {}

    def test_inactive_user_is_rejected(self, api_client, owner):
        owner.is_active = False
        owner.save()

        assert _login(api_client, owner.email).status_code == 401


@pytest.mark.django_db
class TestDevCodeExposure:
    @override_settings(LOCAL=True)
    def test_code_is_returned_locally(self, api_client, owner):
        assert "dev_code" in _login(api_client, owner.email).json()

    @override_settings(LOCAL=False)
    def test_code_is_not_returned_otherwise(self, api_client, owner):
        """
        The source repo returned the code whenever ENV != prod, which exposed
        it in every shared non-production environment.
        """
        assert "dev_code" not in _login(api_client, owner.email).json()


@pytest.mark.django_db
class TestVerify:
    @pytest.fixture(autouse=True)
    def _local_mode(self, settings):
        """LOCAL exposes dev_code, which is how these tests learn the code."""
        settings.LOCAL = True

    def _challenge(self, api_client, user):
        return _login(api_client, user.email).json()["dev_code"]

    def test_correct_code_signs_the_user_in(self, api_client, owner):
        code = self._challenge(api_client, owner)

        response = api_client.post(VERIFY_URL, {"code": code})

        assert response.status_code == 200
        assert response.json()["email"] == owner.email
        assert api_client.get(reverse("users:session")).json()["email"] == owner.email

    def test_wrong_code_is_rejected_and_burns_an_attempt(self, api_client, owner):
        self._challenge(api_client, owner)

        response = api_client.post(VERIFY_URL, {"code": "000000"})

        assert response.status_code == 401
        assert TwoFactorCode.objects.get(user=owner).attempts == 1

    def test_code_cannot_be_reused(self, api_client, owner):
        code = self._challenge(api_client, owner)
        api_client.post(VERIFY_URL, {"code": code})

        assert api_client.post(VERIFY_URL, {"code": code}).status_code == 400

    def test_attempts_are_capped(self, api_client, owner):
        code = self._challenge(api_client, owner)

        responses = [
            api_client.post(VERIFY_URL, {"code": "000000"})
            for _ in range(TwoFactorCode.MAX_ATTEMPTS)
        ]

        assert [r.status_code for r in responses] == [401] * TwoFactorCode.MAX_ATTEMPTS
        assert TwoFactorCode.objects.get(user=owner).attempts == TwoFactorCode.MAX_ATTEMPTS

        # Hitting the cap abandons the challenge entirely: the pending state is
        # cleared, so even the correct code now reads as "no sign-in in
        # progress" rather than as a further guess.
        response = api_client.post(VERIFY_URL, {"code": code})

        assert response.status_code == 400
        assert not api_client.get(reverse("users:session")).json()

    def test_expired_code_is_refused(self, api_client, owner):
        from datetime import timedelta

        from django.utils import timezone

        code = self._challenge(api_client, owner)
        TwoFactorCode.objects.update(expires_at=timezone.now() - timedelta(minutes=1))

        assert api_client.post(VERIFY_URL, {"code": code}).status_code == 401

    def test_verify_without_a_pending_challenge_is_a_400(self, api_client):
        assert api_client.post(VERIFY_URL, {"code": "123456"}).status_code == 400

    def test_resend_issues_a_new_code(self, api_client, owner):
        first = self._challenge(api_client, owner)

        second = api_client.post(RESEND_URL).json()["dev_code"]

        assert TwoFactorCode.objects.filter(user=owner).count() == 2
        # The superseded code no longer works.
        assert api_client.post(VERIFY_URL, {"code": first}).status_code == 401
        assert api_client.post(VERIFY_URL, {"code": second}).status_code == 200


@pytest.mark.django_db
class TestPerRolePolicy:
    """Three audiences, three policies. See docs/DECISIONS.md ADR-008."""

    @pytest.fixture(autouse=True)
    def _local_mode(self, settings):
        settings.LOCAL = True

    def test_cleaner_on_a_new_device_is_challenged(self, api_client, organization, make_member):
        cleaner = make_member(organization, role=Role.CLEANER)

        assert _login(api_client, cleaner.email).status_code == 202

    def test_cleaner_on_a_trusted_device_signs_straight_in(
        self, api_client, organization, make_member
    ):
        cleaner = make_member(organization, role=Role.CLEANER)
        _, raw_token = TrustedDevice.issue(cleaner)
        api_client.cookies[TRUSTED_DEVICE_COOKIE] = raw_token

        response = _login(api_client, cleaner.email)

        assert response.status_code == 200
        assert response.json()["email"] == cleaner.email

    def test_owner_is_challenged_even_on_a_trusted_device(self, api_client, organization, owner):
        _, raw_token = TrustedDevice.issue(owner)
        api_client.cookies[TRUSTED_DEVICE_COOKIE] = raw_token

        assert _login(api_client, owner.email).status_code == 202

    def test_customer_skips_two_factor(self, api_client, organization, make_member):
        customer = make_member(organization, role=Role.CUSTOMER)

        assert _login(api_client, customer.email).status_code == 200

    def test_remember_device_sets_a_cookie(self, api_client, organization, make_member):
        cleaner = make_member(organization, role=Role.CLEANER)
        code = _login(api_client, cleaner.email).json()["dev_code"]

        response = api_client.post(VERIFY_URL, {"code": code, "remember_device": True})

        assert response.status_code == 200
        assert TRUSTED_DEVICE_COOKIE in response.cookies
        assert TrustedDevice.objects.filter(user=cleaner).count() == 1

    def test_expired_trusted_device_does_not_count(self, api_client, organization, make_member):
        from datetime import timedelta

        from django.utils import timezone

        cleaner = make_member(organization, role=Role.CLEANER)
        _, raw_token = TrustedDevice.issue(cleaner)
        TrustedDevice.objects.update(expires_at=timezone.now() - timedelta(days=1))
        api_client.cookies[TRUSTED_DEVICE_COOKIE] = raw_token

        assert _login(api_client, cleaner.email).status_code == 202


@pytest.mark.django_db
class TestCodeStorage:
    def test_the_raw_code_is_never_stored(self, api_client, owner):
        with override_settings(LOCAL=True):
            code = _login(api_client, owner.email).json()["dev_code"]

        challenge = TwoFactorCode.objects.get(user=owner)

        assert code not in challenge.code_hash
        assert challenge.code_hash != code
