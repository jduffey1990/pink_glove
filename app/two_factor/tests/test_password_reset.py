"""
Staff password reset.

Two public endpoints, so each has one job to get exactly right. Asking must
say the same thing to everyone, in the same time, and only ever email staff.
Confirming must leave nothing from before the reset working: no session, no
trusted device, no half-finished sign-in. And it must not sign anyone in or
excuse them the second factor.
"""

import hashlib
import re
from datetime import timedelta

import pytest
from django.core import mail
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework.throttling import SimpleRateThrottle

from two_factor import tasks
from two_factor.models import TrustedDevice, TwoFactorCode
from two_factor.services import INVALID_RESET_LINK, TRUSTED_DEVICE_COOKIE
from two_factor.views import RESET_REQUESTED
from users.enums import AccountTokenPurpose, Role
from users.models import AccountToken, CustomUser, MagicLinkToken, Membership

pytestmark = pytest.mark.django_db

REQUEST = reverse("two_factor:password-reset")
CONFIRM = reverse("two_factor:password-reset-confirm")
LOGIN = reverse("two_factor:login")
VERIFY = reverse("two_factor:verify")
RESEND = reverse("two_factor:resend")
SESSION = reverse("users:session")
DIRECTORY = reverse("users:membership-list")

OLD_PASSWORD = "pw-for-tests-only"  # noqa: S105
NEW_PASSWORD = "a new phrase for tests only"  # noqa: S105
STAFF = (Role.OWNER, Role.ADMIN, Role.DISPATCHER, Role.CLEANER)


def _ask(client, email, **extra):
    return client.post(REQUEST, {"email": email}, format="json", **extra)


def _confirm(client, token, password=NEW_PASSWORD):
    return client.post(CONFIRM, {"token": token, "password": password}, format="json")


def _emailed_token() -> str:
    return re.search(r"/reset-password#token=([\w-]+)", mail.outbox[-1].body).group(1)


def _token_for(user) -> str:
    """Straight from the model, for tests that are not about the request."""
    _, raw = AccountToken.issue(AccountTokenPurpose.PASSWORD_RESET, user)
    return raw


@pytest.fixture
def cleaner(organization, make_member):
    return make_member(organization, role=Role.CLEANER)


@pytest.fixture
def customer(organization):
    user = CustomUser.objects.create_user(email="homeowner@example.com")
    Membership.objects.create(user=user, organization=organization, role=Role.CUSTOMER)
    return user


class TestAsking:
    def test_staff_are_emailed_a_link(self, api_client, owner, settings):
        settings.FRONTEND_BASE_URL = "https://app.example.test"

        response = _ask(api_client, owner.email)

        assert response.status_code == 202
        assert len(mail.outbox) == 1
        message = mail.outbox[0]
        assert message.to == [owner.email]
        assert "Reset your" in message.subject
        # The token rides in the fragment, never the path (ADR-036).
        assert "https://app.example.test/reset-password#token=" in message.body
        assert "/reset-password/" not in message.body
        assert "60 minutes" in message.body

    @pytest.mark.parametrize("role", STAFF)
    def test_every_staff_role_may_reset(self, api_client, organization, make_member, role):
        _ask(api_client, make_member(organization, role=role).email)

        assert len(mail.outbox) == 1

    def test_the_address_is_case_insensitive(self, api_client, owner):
        _ask(api_client, owner.email.upper())

        assert [message.to for message in mail.outbox] == [[owner.email]]

    def test_a_customer_gets_no_email(self, api_client, customer):
        """They have no password. Their way in is a magic link."""
        _ask(api_client, customer.email)

        assert mail.outbox == []
        assert AccountToken.objects.count() == 0

    def test_staff_somewhere_and_a_customer_elsewhere_is_staff(
        self, api_client, customer, other_organization
    ):
        Membership.objects.create(user=customer, organization=other_organization, role=Role.CLEANER)

        _ask(api_client, customer.email)

        assert len(mail.outbox) == 1

    def test_a_platform_superuser_gets_no_email(self, api_client, superuser, organization):
        """Even one who is also somebody's owner: that password is changed from a shell."""
        Membership.objects.create(user=superuser, organization=organization, role=Role.OWNER)

        _ask(api_client, superuser.email)

        assert mail.outbox == []

    def test_nobody_gets_no_email(self, api_client):
        _ask(api_client, "nobody@example.com")

        assert mail.outbox == []

    def test_someone_deactivated_everywhere_gets_no_email(self, api_client, owner):
        Membership.objects.filter(user=owner).update(is_active=False)

        _ask(api_client, owner.email)

        assert mail.outbox == []

    def test_a_switched_off_account_gets_no_email(self, api_client, owner):
        CustomUser.objects.filter(pk=owner.pk).update(is_active=False)

        _ask(api_client, owner.email)

        assert mail.outbox == []

    def test_an_invitation_nobody_accepted_is_not_a_way_in(self, api_client, organization):
        """A reset must not stand in for accepting: the invitee is not staff yet."""
        invited = CustomUser.objects.create_user(email="invited@example.com")
        Membership.objects.create(
            user=invited,
            organization=organization,
            role=Role.ADMIN,
            is_active=False,
            invited_at=timezone.now(),
        )

        _ask(api_client, invited.email)

        assert mail.outbox == []

    def test_only_the_hash_is_stored_and_it_lasts_an_hour(self, api_client, owner):
        _ask(api_client, owner.email)

        raw = _emailed_token()
        token = AccountToken.objects.get()
        assert token.purpose == AccountTokenPurpose.PASSWORD_RESET
        assert token.token_hash == hashlib.sha256(raw.encode()).hexdigest()
        assert token.membership is None
        assert abs((token.expires_at - token.created_at).total_seconds() - 3600) < 5

    def test_asking_again_kills_the_earlier_link(self, api_client, owner):
        _ask(api_client, owner.email)
        first = _emailed_token()
        _ask(api_client, owner.email)
        second = _emailed_token()

        assert _confirm(api_client, first).status_code == 400
        assert _confirm(api_client, second).status_code == 200

    def test_a_bad_address_is_a_400_for_everyone(self, api_client):
        response = _ask(api_client, "not-an-address")

        assert response.status_code == 400
        assert "email" in response.json()

    @pytest.mark.parametrize("body", [["a"], {"email": 5}, {"email": ["a@example.com"]}, {}, "x"])
    def test_a_malformed_body_is_a_400_not_a_500(self, api_client, body):
        """The per-address throttle reads the body before any serializer has."""
        api_client.raise_request_exception = False

        assert api_client.post(REQUEST, body, format="json").status_code == 400


class TestAskingSaysTheSameToEveryone:
    """Status, body and work done must not depend on the address."""

    @pytest.fixture
    def addresses(self, owner, cleaner, customer, superuser):
        deactivated = CustomUser.objects.create_user(
            email="left@example.com",
            password=OLD_PASSWORD,
        )
        Membership.objects.create(
            user=deactivated, organization=owner.memberships.get().organization, role=Role.ADMIN
        )
        Membership.objects.filter(user=deactivated).update(is_active=False)
        return [
            owner.email,
            cleaner.email,
            customer.email,
            superuser.email,
            deactivated.email,
            "nobody@example.com",
        ]

    def test_the_status_and_body_are_identical(self, addresses):
        responses = [_ask(APIClient(), address) for address in addresses]

        assert {response.status_code for response in responses} == {202}
        assert {response.content for response in responses} == {
            b'{"detail":"' + RESET_REQUESTED.encode() + b'"}'
        }
        # Guards the guard: two of those addresses really were emailed.
        assert len(mail.outbox) == 2

    def test_no_cookie_or_header_gives_it_away(self, addresses):
        responses = [_ask(APIClient(), address) for address in addresses]

        assert len({tuple(sorted(response.headers.items())) for response in responses}) == 1
        assert all(not response.cookies for response in responses)

    def test_the_request_itself_never_looks_the_account_up(
        self, addresses, monkeypatch, django_assert_num_queries
    ):
        """
        The timing half. Every address costs the request the same: no query
        and one task. Whether an account exists is only found out in the
        worker, where no caller can time it.
        """
        handed_over = []
        monkeypatch.setattr(tasks.send_password_reset, "delay", handed_over.append)

        for address in addresses:
            with django_assert_num_queries(0):
                assert _ask(APIClient(), address).status_code == 202

        assert handed_over == addresses
        assert mail.outbox == []

    def test_the_task_reports_nothing(self, owner):
        """No return value to log and no result row, for staff or for nobody."""
        assert tasks.send_password_reset(owner.email) is None
        assert tasks.send_password_reset("nobody@example.com") is None
        assert tasks.send_password_reset.ignore_result is True


class TestTheLinkIsOnlyReturnedLocally:
    def test_it_is_absent_in_a_deployed_environment(self, api_client, owner):
        response = _ask(api_client, owner.email)

        assert response.json().keys() == {"detail"}
        assert len(mail.outbox) == 1

    def test_it_is_present_when_running_locally(self, api_client, owner, settings):
        settings.LOCAL = True

        response = _ask(api_client, owner.email)

        assert response.json()["dev_link"].startswith(
            f"{settings.FRONTEND_BASE_URL}/reset-password#token="
        )
        # Mirrors `dev_code`: locally the link is in the response, not a mailbox.
        assert mail.outbox == []

    def test_locally_a_customer_still_gets_nothing(self, api_client, customer, settings):
        settings.LOCAL = True

        assert _ask(api_client, customer.email).json().keys() == {"detail"}


class TestNothingSensitiveIsLogged:
    def test_not_on_success(self, api_client, owner, caplog):
        with caplog.at_level("DEBUG"):
            _ask(api_client, owner.email)
            token = _emailed_token()
            _confirm(api_client, token)

        assert token not in caplog.text
        assert "reset-password" not in caplog.text
        assert NEW_PASSWORD not in caplog.text

    def test_not_when_the_mail_fails(self, api_client, owner, monkeypatch, caplog):
        def boom(self, fail_silently=False):
            raise OSError("smtp is down")

        monkeypatch.setattr("django.core.mail.EmailMessage.send", boom)

        with caplog.at_level("DEBUG"):
            response = _ask(api_client, owner.email)

        # Still the same answer: a mail failure must not be an oracle either.
        assert response.status_code == 202
        assert response.json() == {"detail": RESET_REQUESTED}
        assert "Failed to send password reset" in caplog.text
        assert "reset-password/" not in caplog.text
        assert owner.email not in caplog.text


class TestConfirming:
    def test_it_sets_the_password(self, api_client, owner):
        response = _confirm(api_client, _token_for(owner))

        owner.refresh_from_db()
        assert response.status_code == 200
        assert response.json().keys() == {"detail"}
        assert owner.check_password(NEW_PASSWORD)
        assert not owner.check_password(OLD_PASSWORD)

    def test_it_does_not_sign_them_in(self, api_client, owner):
        _confirm(api_client, _token_for(owner))

        assert api_client.get(SESSION).json() == {}
        assert api_client.get(DIRECTORY).status_code == 403

    @pytest.mark.parametrize("role", STAFF)
    def test_the_next_sign_in_still_meets_the_challenge(
        self, api_client, organization, make_member, role, settings
    ):
        """A reset is not a way around the second factor (ADR-008)."""
        settings.LOCAL = True
        user = make_member(organization, role=role)
        _confirm(api_client, _token_for(user))

        login = api_client.post(
            LOGIN, {"email": user.email, "password": NEW_PASSWORD}, format="json"
        )

        assert login.status_code == 202
        assert login.json()["two_factor_required"] is True
        assert api_client.get(SESSION).json() == {}
        assert (
            api_client.post(VERIFY, {"code": login.json()["dev_code"]}, format="json").status_code
            == 200
        )

    def test_the_old_password_no_longer_signs_in(self, api_client, owner):
        _confirm(api_client, _token_for(owner))

        login = api_client.post(
            LOGIN, {"email": owner.email, "password": OLD_PASSWORD}, format="json"
        )

        assert login.status_code == 401

    @pytest.mark.parametrize(
        ("password", "fragment"),
        [
            ("short1!", "too short"),
            ("password", "too common"),
            ("8675309867", "entirely numeric"),
            ("owner-sparkle-clean", "too similar"),
        ],
    )
    def test_the_password_goes_through_the_validators(self, api_client, owner, password, fragment):
        response = _confirm(api_client, _token_for(owner), password=password)

        owner.refresh_from_db()
        assert response.status_code == 400
        assert fragment in " ".join(response.json()["password"])
        assert owner.check_password(OLD_PASSWORD)

    def test_a_refused_password_does_not_cost_the_link(self, api_client, owner):
        token = _token_for(owner)
        assert _confirm(api_client, token, password="password").status_code == 400

        assert _confirm(api_client, token).status_code == 200

    def test_a_refused_password_ends_no_session(self, authed_client, api_client, owner):
        _confirm(APIClient(), _token_for(owner), password="password")

        assert authed_client.get(SESSION).json()["email"] == owner.email

    def test_a_password_is_required(self, api_client, owner):
        response = api_client.post(CONFIRM, {"token": _token_for(owner)}, format="json")

        assert response.status_code == 400
        assert "password" in response.json()


class TestTheTokenIsSingleUseAndBound:
    def test_it_works_once(self, api_client, owner):
        token = _token_for(owner)
        assert _confirm(api_client, token).status_code == 200

        again = _confirm(api_client, token, password="yet another phrase for tests")

        owner.refresh_from_db()
        assert again.status_code == 400
        assert again.json() == {"detail": INVALID_RESET_LINK}
        assert owner.check_password(NEW_PASSWORD)

    def test_it_expires_after_an_hour(self, api_client, owner):
        token = _token_for(owner)
        AccountToken.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

        response = _confirm(api_client, token)

        owner.refresh_from_db()
        assert response.status_code == 400
        assert owner.check_password(OLD_PASSWORD)

    def test_every_refusal_reads_the_same(self, api_client, owner, cleaner):
        used = _token_for(owner)
        _confirm(api_client, used)
        expired = _token_for(cleaner)
        AccountToken.objects.filter(user=cleaner).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )

        bodies = [_confirm(api_client, token).json() for token in ("unknown", used, expired)]

        assert bodies == [{"detail": INVALID_RESET_LINK}] * 3

    def test_an_invitation_is_not_a_reset(self, api_client, organization):
        """Or an unaccepted invitee could set a password and skip accepting."""
        invited = CustomUser.objects.create_user(email="invited@example.com")
        membership = Membership.objects.create(
            user=invited,
            organization=organization,
            role=Role.OWNER,
            is_active=False,
            invited_at=timezone.now(),
        )
        _, invite = AccountToken.issue(AccountTokenPurpose.INVITE, invited, membership)

        response = _confirm(api_client, invite)

        invited.refresh_from_db()
        membership.refresh_from_db()
        assert response.status_code == 400
        assert invited.has_usable_password() is False
        assert membership.is_active is False
        # And trying it did not spend the invitation.
        assert AccountToken.find(AccountTokenPurpose.INVITE, invite) is not None

    def test_an_invitation_for_someone_already_staff_is_not_a_reset_either(
        self, api_client, owner, other_organization
    ):
        """The sharper case: this user *may* reset, so only the purpose stops it."""
        membership = Membership.objects.create(
            user=owner,
            organization=other_organization,
            role=Role.CLEANER,
            is_active=False,
            invited_at=timezone.now(),
        )
        _, invite = AccountToken.issue(AccountTokenPurpose.INVITE, owner, membership)

        response = _confirm(api_client, invite)

        owner.refresh_from_db()
        assert response.status_code == 400
        assert owner.check_password(OLD_PASSWORD)

    def test_a_magic_link_is_not_a_reset(self, api_client, owner):
        _, magic = MagicLinkToken.issue(owner)

        response = _confirm(api_client, magic)

        owner.refresh_from_db()
        assert response.status_code == 400
        assert owner.check_password(OLD_PASSWORD)

    def test_a_reset_is_not_a_magic_link(self, api_client, owner):
        """The one that would matter most: a reset link must not sign anyone in."""
        token = _token_for(owner)

        response = api_client.post(
            reverse("users:magic-link-consume"), {"token": token}, format="json"
        )

        assert response.status_code == 401
        assert api_client.get(SESSION).json() == {}

    def test_it_is_refused_once_the_person_is_deactivated(self, api_client, owner):
        """Asked again at the end: a link outlives the request that issued it."""
        token = _token_for(owner)
        Membership.objects.filter(user=owner).update(is_active=False)

        response = _confirm(api_client, token)

        owner.refresh_from_db()
        assert response.status_code == 400
        assert owner.check_password(OLD_PASSWORD)


class TestEverythingFromBeforeTheResetIsGone:
    def test_every_open_session_ends(self, owner):
        laptop, phone = APIClient(), APIClient()
        laptop.force_login(owner)
        phone.force_login(owner)
        assert laptop.get(DIRECTORY).status_code == 200

        _confirm(APIClient(), _token_for(owner))

        for device in (laptop, phone):
            assert device.get(SESSION).json() == {}
            assert device.get(DIRECTORY).status_code == 403

    def test_someone_elses_session_does_not(self, owner, cleaner):
        theirs = APIClient()
        theirs.force_login(cleaner)

        _confirm(APIClient(), _token_for(owner))

        assert theirs.get(SESSION).json()["email"] == cleaner.email

    def test_every_trusted_device_is_forgotten(self, api_client, cleaner, settings):
        """A cleaner's trusted phone skipped the code before. Not after."""
        settings.LOCAL = True
        _, device = TrustedDevice.issue(cleaner)
        api_client.cookies[TRUSTED_DEVICE_COOKIE] = device
        before = api_client.post(
            LOGIN, {"email": cleaner.email, "password": OLD_PASSWORD}, format="json"
        )
        assert before.status_code == 200
        api_client.post(reverse("users:logout"))

        _confirm(APIClient(), _token_for(cleaner))
        after = api_client.post(
            LOGIN, {"email": cleaner.email, "password": NEW_PASSWORD}, format="json"
        )

        assert after.status_code == 202
        assert TrustedDevice.objects.filter(user=cleaner).count() == 0

    def test_someone_elses_trusted_device_is_not(self, owner, cleaner):
        _, device = TrustedDevice.issue(cleaner)

        _confirm(APIClient(), _token_for(owner))

        assert TrustedDevice.is_trusted(cleaner, device) is True

    def test_a_sign_in_started_with_the_old_password_cannot_be_finished(
        self, api_client, owner, settings
    ):
        """Password accepted, code pending, and then the reset lands."""
        settings.LOCAL = True
        login = api_client.post(
            LOGIN, {"email": owner.email, "password": OLD_PASSWORD}, format="json"
        )
        code = login.json()["dev_code"]

        _confirm(APIClient(), _token_for(owner))
        verify = api_client.post(VERIFY, {"code": code}, format="json")

        assert verify.status_code == 401
        assert api_client.get(SESSION).json() == {}

    def test_nor_can_it_ask_for_a_fresh_code_and_finish_with_that(
        self, api_client, owner, settings
    ):
        """
        The way round the test above. The pending sign-in remembers only that
        *a* password was accepted, so asking for a new code used to hand the
        old password's holder a live challenge after the reset, and with it
        a sign-in the new password was never asked for.
        """
        settings.LOCAL = True
        api_client.post(LOGIN, {"email": owner.email, "password": OLD_PASSWORD}, format="json")

        _confirm(APIClient(), _token_for(owner))
        resend = api_client.post(RESEND)

        assert resend.status_code == 400
        assert "dev_code" not in resend.json()
        # Nothing was issued, and the pending sign-in is over: start again.
        assert TwoFactorCode.objects.filter(user=owner, consumed_at__isnull=True).count() == 0
        assert api_client.post(RESEND).status_code == 400
        assert api_client.post(VERIFY, {"code": "000000"}, format="json").status_code == 400
        assert api_client.get(SESSION).json() == {}

    def test_asking_for_a_fresh_code_still_works_when_there_was_no_reset(
        self, api_client, owner, settings
    ):
        """The ordinary resend, including after the first code has expired."""
        settings.LOCAL = True
        api_client.post(LOGIN, {"email": owner.email, "password": OLD_PASSWORD}, format="json")
        TwoFactorCode.objects.filter(user=owner).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )

        resend = api_client.post(RESEND)

        assert resend.status_code == 202
        verify = api_client.post(VERIFY, {"code": resend.json()["dev_code"]}, format="json")
        assert verify.status_code == 200


class TestThrottles:
    @pytest.fixture(autouse=True)
    def _tight_rates(self, monkeypatch, settings):
        rates = dict(settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"])
        rates.update(
            {
                "password_reset": "4/hour",
                "password_reset_account": "2/hour",
                "password_reset_confirm": "3/hour",
            }
        )
        monkeypatch.setattr(SimpleRateThrottle, "THROTTLE_RATES", rates)
        cache.clear()
        yield
        cache.clear()

    def test_one_client_runs_out_whatever_it_asks_about(self, api_client):
        statuses = [_ask(api_client, f"person-{n}@example.com").status_code for n in range(5)]

        assert statuses == [202, 202, 202, 202, 429]

    def test_a_forged_forwarded_header_does_not_buy_a_fresh_bucket(self, api_client):
        statuses = [
            _ask(
                api_client, f"person-{n}@example.com", HTTP_X_FORWARDED_FOR=f"10.0.0.{n}"
            ).status_code
            for n in range(5)
        ]

        assert statuses[-1] == 429

    def test_one_address_runs_out_whatever_client_asks(self, api_client, owner):
        statuses = [
            _ask(api_client, owner.email, REMOTE_ADDR=f"203.0.113.{n}").status_code
            for n in range(3)
        ]

        assert statuses == [202, 202, 429]
        assert len(mail.outbox) == 2

    def test_an_address_with_no_account_runs_out_just_the_same(self, api_client):
        """Or the 429 itself would say which addresses are real."""
        statuses = [
            _ask(api_client, "nobody@example.com", REMOTE_ADDR=f"203.0.113.{n}").status_code
            for n in range(3)
        ]

        assert statuses == [202, 202, 429]

    def test_case_and_padding_do_not_buy_a_fresh_bucket(self, api_client, owner):
        variants = [owner.email, owner.email.upper(), f"  {owner.email.title()} "]

        statuses = [
            _ask(api_client, variant, REMOTE_ADDR=f"203.0.113.{n}").status_code
            for n, variant in enumerate(variants)
        ]

        assert statuses == [202, 202, 429]

    def test_another_address_is_unaffected(self, api_client, owner, cleaner):
        for n in range(3):
            _ask(api_client, owner.email, REMOTE_ADDR=f"203.0.113.{n}")

        assert _ask(api_client, cleaner.email, REMOTE_ADDR="198.51.100.1").status_code == 202

    def test_using_up_someones_allowance_still_leaves_them_a_working_link(self, api_client, owner):
        """The stranger's own requests are what emailed it."""
        for n in range(3):
            _ask(api_client, owner.email, REMOTE_ADDR=f"203.0.113.{n}")

        assert _confirm(APIClient(), _emailed_token()).status_code == 200

    def test_confirming_is_limited_per_client(self, api_client):
        statuses = [_confirm(api_client, f"guess-{n}").status_code for n in range(4)]

        assert statuses == [400, 400, 400, 429]
