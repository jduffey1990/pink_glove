"""
Error tracking (ADR-029): what an event may carry when it leaves.

Three states, like any integration that can send (Stripe is the precedent):

- no DSN: nothing is initialised and `sentry_sdk` is never imported;
- a DSN, with a transport that keeps what it is given: a view that raises is
  reported, and every credential planted in the request comes out removed;
- production settings import cleanly with the audit's placeholders
  (`test_settings_production.py` runs `bin/deploy-audit.sh`).

The SDK is only ever started in a child process (`sentry_probe.py` says why),
so the tests here that need a real event read the child's report.
"""

import json
import os
import subprocess
import sys
import uuid
from types import SimpleNamespace

import pytest
from django.conf import settings

from app import sentry
from app.sentry import FILTERED, before_send, is_sensitive, scrub_url
from app.tests.sentry_probe import DSN, MARKER, PLANTED
from app.tests.test_settings_production import _BASELINE as PRODUCTION_BASELINE

#: What production.py needs to import at all: the settings tests' own
#: baseline, plus everything optional set EMPTY. A child process cannot have
#: decouple's `.env` lookup patched out the way those tests do, and an empty
#: value in the environment wins over a developer's `app/.env`.
PRODUCTION_ENV = {
    **PRODUCTION_BASELINE,
    "UI_DIST_DIR": "",
    "STRIPE_SECRET_KEY": "",
    "STRIPE_PLATFORM_PRICE_ID": "",
    "SENTRY_DSN": "",
    "SENTRY_ENVIRONMENT": "",
    "SENTRY_RELEASE": "",
    "FLY_IMAGE_REF": "",
}


def run_probe(mode: str, **env) -> dict:
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "app.tests.sentry_probe", mode],
        cwd=settings.BASE_DIR,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    (line,) = [line for line in completed.stdout.splitlines() if line.startswith(MARKER)]
    return json.loads(line[len(MARKER) :])


@pytest.fixture(scope="module")
def captured() -> dict:
    return run_probe("capture")


def events(envelopes: list) -> list[dict]:
    return [item["payload"] for item in envelopes if item["type"] == "event"]


class TestWhatCountsAsSensitive:
    @pytest.mark.parametrize(
        "name",
        [
            # The three encrypted columns (ADR-014) and the notes beside them.
            "gate_code",
            "alarm_code",
            "key_location",
            "access_notes",
            "access_code",
            # However a client or a header spells it.
            "gateCode",
            "GATE-CODE",
            "Gate Code",
            # The second factor is posted as plain `code`; the API returns
            # `dev_code` in development.
            "code",
            "dev_code",
            "token",
            "csrfmiddlewaretoken",
            "password",
            "new_password",
            "EMAIL_HOST_PASSWORD",
            "Cookie",
            "Set-Cookie",
            "sessionid",
            "csrftoken",
            "X-CSRFToken",
            "Authorization",
            "Proxy-Authorization",
            "Stripe-Signature",
            "STRIPE_SECRET_KEY",
            # Both webhook endpoints' signing secrets (Phase 4b, Phase 4c).
            "STRIPE_CONNECT_WEBHOOK_SECRET",
            "STRIPE_PLATFORM_WEBHOOK_SECRET",
            "FIELD_ENCRYPTION_KEY",
            "SENTRY_DSN",
            "AWS_SECRET_ACCESS_KEY",
            "api_key",
            "pin",
            # Not credentials, but who was asking.
            "REMOTE_ADDR",
            "X-Forwarded-For",
            "Fly-Client-IP",
            "ip_address",
        ],
    )
    def test_these_never_leave(self, name):
        assert is_sensitive(name)

    @pytest.mark.parametrize(
        "name",
        [
            # What makes an event worth reading.
            "status_code",
            "http.response.status_code",
            "url",
            "method",
            "Content-Type",
            "User-Agent",
            "transaction",
            "filename",
            "lineno",
            "celery_task_id",
            "task_name",
            # "gate" and "key" are words here, not substrings.
            "navigate",
            "aggregate",
            "monkey",
            "organization_id",
            # Stripe's identifiers for a tenant (Phase 4c) are names, not
            # credentials: nothing can be done with a `cus_` or `sub_` id
            # without the platform's key. Left readable on purpose, like the
            # `pi_` and `evt_` ids the webhook already logs, because they are
            # what an error about a subscription is looked up by.
            "stripe_customer_id",
            "stripe_subscription_id",
            "stripe_account_id",
            "STRIPE_PLATFORM_PRICE_ID",
            "subscription_status",
        ],
    )
    def test_and_these_do(self, name):
        assert not is_sensitive(name)

    def test_a_key_that_is_not_a_string_is_not_a_crash(self):
        assert is_sensitive(42) is False


class TestCredentialsInAUrl:
    def test_a_named_query_value_is_redacted_and_the_rest_kept(self):
        assert (
            scrub_url("https://app.test/sign-in?token=abc123&page=2")
            == f"https://app.test/sign-in?token={FILTERED}&page=2"
        )

    def test_a_uuid_in_a_path_is_an_identifier_and_stays(self):
        url = f"/api/billing/invoices/{uuid.uuid4()}/void/"

        assert scrub_url(url) == url

    def test_a_stripe_hosted_session_url_loses_the_session_it_names(self):
        """
        What `billing.subscription` gets back from Stripe (Phase 4c): the
        billing portal's URL signs its holder in to the tenant's subscription,
        and a Checkout URL carries its session. Neither is logged today; if
        one ever lands in a log line or an exception, the session goes.
        """
        portal = "https://billing.stripe.com/p/session/test_" + "YWNjdF8xVGVzdFBsYXRmb3Jt" * 3
        checkout = "https://checkout.stripe.com/c/pay/cs_test_" + "a1B2c3D4e5F6g7H8i9J0" * 3

        assert scrub_url(f"portal: {portal}") == (
            f"portal: https://billing.stripe.com/p/session/{FILTERED}"
        )
        assert scrub_url(checkout) == f"https://checkout.stripe.com/c/pay/{FILTERED}"

    def test_the_pay_links_token_is_redacted_wherever_the_url_appears(self):
        """`billing.connect.pay_token`: the credential in an invoice email."""
        from billing.connect import pay_token

        token = pay_token(SimpleNamespace(pk=uuid.uuid4()))

        assert scrub_url(f"/api/billing/pay/{token}/") == f"/api/billing/pay/{FILTERED}/"
        assert (
            scrub_url(f"/api/billing/pay/{token}/checkout/")
            == f"/api/billing/pay/{FILTERED}/checkout/"
        )
        assert scrub_url(f"Not Found: /pay/{token}") == f"Not Found: /pay/{FILTERED}"

    @pytest.mark.django_db
    def test_a_customers_magic_link_is_redacted(self, user):
        from users.models import MagicLinkToken

        _, raw_token = MagicLinkToken.issue(user)

        assert scrub_url(f"https://app.test/sign-in/{raw_token}") == (
            f"https://app.test/sign-in/{FILTERED}"
        )


class TestBeforeSend:
    """The scrubber alone, on events shaped like the SDK's."""

    def test_the_request_body_and_cookies_are_removed_outright(self):
        event = {
            "request": {
                "url": "https://app.test/api/customers/locations/",
                "data": {"gate_code": "4821#", "address_line1": "12 Elm St"},
                "cookies": {"sessionid": "abc"},
            }
        }

        assert before_send(event, {})["request"] == {
            "url": "https://app.test/api/customers/locations/"
        }

    def test_a_body_that_is_not_json_goes_too(self):
        event = {"request": {"data": "gate_code=4821%23&alarm_code=9930"}}

        assert before_send(event, {})["request"] == {}

    def test_every_local_variable_is_removed_whatever_it_is_called(self):
        frame = {"function": "reveal", "lineno": 7, "vars": {"value": "'4821#'", "n": "3"}}
        event = {"exception": {"values": [{"type": "X", "stacktrace": {"frames": [frame]}}]}}

        scrubbed = before_send(event, {})

        assert scrubbed["exception"]["values"][0]["stacktrace"]["frames"] == [
            {"function": "reveal", "lineno": 7}
        ]

    def test_a_sensitive_name_is_redacted_at_any_depth(self):
        event = {
            "extra": {"celery-job": {"kwargs": {"gate_code": "4821#", "organization_id": "o1"}}},
            "breadcrumbs": {"values": [{"data": {"alarm_code": "9930", "status_code": 200}}]},
            "contexts": {"state": {"key_location": "under the mat"}},
            "tags": {"token": "t0k3n", "celery_task_id": "abc"},
        }

        scrubbed = before_send(event, {})

        assert scrubbed["extra"]["celery-job"]["kwargs"] == {
            "gate_code": FILTERED,
            "organization_id": "o1",
        }
        assert scrubbed["breadcrumbs"]["values"][0]["data"] == {
            "alarm_code": FILTERED,
            "status_code": 200,
        }
        assert scrubbed["contexts"]["state"] == {"key_location": FILTERED}
        assert scrubbed["tags"] == {"token": FILTERED, "celery_task_id": "abc"}

    def test_a_sensitive_name_holding_a_structure_loses_all_of_it(self):
        event = {"extra": {"access": {"gate": "4821#", "notes": ["side door"]}}}

        assert before_send(event, {})["extra"] == {"access": FILTERED}

    def test_an_empty_value_is_left_empty_so_absence_stays_visible(self):
        event = {"extra": {"gate_code": "", "alarm_code": None}}

        assert before_send(event, {})["extra"] == {"gate_code": "", "alarm_code": None}

    def test_headers_lose_credentials_and_the_callers_address(self):
        event = {
            "request": {
                "headers": {
                    "Authorization": "Bearer abc",
                    "Cookie": "sessionid=abc",
                    "X-Csrftoken": "abc",
                    "Stripe-Signature": "t=1,v1=abc",
                    "Fly-Client-Ip": "203.0.113.77",
                    "User-Agent": "Mozilla/5.0",
                    "X-Organization": "5f0c1e3a-3c0e-4b6b-9d53-1f0a3c1f9b77",
                },
                "env": {"REMOTE_ADDR": "203.0.113.77", "SERVER_NAME": "app.test"},
            }
        }

        request = before_send(event, {})["request"]

        assert request["headers"] == {
            "Authorization": FILTERED,
            "Cookie": FILTERED,
            "X-Csrftoken": FILTERED,
            "Stripe-Signature": FILTERED,
            "Fly-Client-Ip": FILTERED,
            "User-Agent": "Mozilla/5.0",
            "X-Organization": "5f0c1e3a-3c0e-4b6b-9d53-1f0a3c1f9b77",
        }
        assert request["env"] == {"REMOTE_ADDR": FILTERED, "SERVER_NAME": "app.test"}

    def test_the_query_string_field_has_no_question_mark_and_is_still_scrubbed(self):
        event = {"request": {"query_string": "token=abc123&page=2"}}

        assert before_send(event, {})["request"]["query_string"] == f"token={FILTERED}&page=2"

    def test_the_user_is_removed_whole_not_even_an_id_is_kept(self):
        event = {"user": {"id": "u1", "email": "pat@example.com", "ip_address": "203.0.113.77"}}

        assert before_send(event, {}) == {}

    def test_a_long_file_name_in_a_stack_frame_is_not_mistaken_for_a_token(self):
        """It is the line someone needs to read when a migration fails."""
        path = "/code/billing/migrations/0042_a_migration_with_a_long_and_descriptive_name.py"
        event = {"exception": {"values": [{"stacktrace": {"frames": [{"abs_path": path}]}}]}}

        scrubbed = before_send(event, {})

        assert scrubbed["exception"]["values"][0]["stacktrace"]["frames"][0]["abs_path"] == path

    def test_the_release_and_environment_are_left_exactly_as_tagged(self):
        release = "registry.fly.io/pink-glove:deployment-01JABCDEFGHJKMNPQRSTVWXYZ0123456789"
        event = {"release": release, "environment": "production"}

        assert before_send(event, {}) == {"release": release, "environment": "production"}

    def test_the_suite_itself_never_starts_the_sdk(self):
        """Importing `app.sentry` and scrubbing with it must not import it."""
        assert sentry.init is not None
        assert "sentry_sdk" not in sys.modules


class TestAnEventAsProductionSendsIt:
    """`app.sentry.init`, a request to a view that raises, and what came out."""

    def test_the_view_the_task_and_the_error_log_are_each_reported(self, captured):
        reported = events(captured["shipped"])

        assert [event.get("transaction") for event in reported] == [
            "/probe/{token}/boom/",
            "probe.boom",
            None,
        ]
        assert reported[0]["exception"]["values"][-1]["type"] == "RuntimeError"
        assert reported[0]["exception"]["values"][-1]["mechanism"]["type"] == "django"
        assert reported[1]["exception"]["values"][-1]["mechanism"]["type"] == "celery"
        assert reported[2]["logentry"]["message"] == "probe log line"

    def test_nothing_but_error_events_leaves(self, captured):
        """No sessions, no traces, no client reports: `before_send` sees it all."""
        assert {item["type"] for item in captured["shipped"]} == {"event"}

    def test_every_event_is_tagged_with_its_environment_and_release(self, captured):
        for event in events(captured["shipped"]):
            assert event["environment"] == "probe"
            assert event["release"] == "probe-release"

    @pytest.mark.parametrize("planted", sorted(PLANTED))
    def test_no_planted_credential_is_anywhere_in_it(self, captured, planted):
        assert PLANTED[planted] not in json.dumps(captured["shipped"])

    def test_the_request_is_still_recognisable(self, captured):
        request = events(captured["shipped"])[0]["request"]

        assert request["method"] == "POST"
        assert request["url"] == f"http://testserver/probe/{FILTERED}/boom/"
        assert request["query_string"] == f"token={FILTERED}&page=2"
        assert request["headers"]["Referer"] == f"https://testserver/pay/{FILTERED}"
        assert "data" not in request
        assert "cookies" not in request

    def test_no_frame_carries_local_variables_and_no_event_a_user(self, captured):
        for event in events(captured["shipped"]):
            assert not event.get("user")
            for exception in event.get("exception", {}).get("values", []):
                for frame in exception["stacktrace"]["frames"]:
                    assert "vars" not in frame

    def test_a_querys_text_is_kept_as_a_breadcrumb_without_what_was_bound_to_it(self, captured):
        """ADR-029 lists this: the statement, placeholders in place of values."""
        crumbs = events(captured["shipped"])[0]["breadcrumbs"]["values"]

        assert "select %s" in [crumb["message"] for crumb in crumbs if crumb["category"] == "query"]

    def test_the_task_is_named_but_its_arguments_are_not_sent(self, captured):
        job = events(captured["shipped"])[1]["extra"]["celery-job"]

        assert job == {"task_name": "probe.boom", "args": FILTERED, "kwargs": FILTERED}


class TestTheScrubberOnItsOwn:
    """
    The SDK told to collect everything, so `before_send` is the only thing
    between the planted values and the wire: the case of an option flipped
    later, or a default that changes in an upgrade.
    """

    @pytest.mark.parametrize("planted", sorted(PLANTED))
    def test_no_planted_credential_gets_past_it(self, captured, planted):
        assert len(events(captured["loosened"])) == 3
        assert PLANTED[planted] not in json.dumps(events(captured["loosened"]))

    @pytest.mark.parametrize(
        "planted",
        ["body", "local", "query", "csrf", "task", "log_extra", "path", "referer", "client_ip"],
    )
    def test_and_without_it_each_one_is_there_to_be_seen(self, captured, planted):
        """
        The control. If this fails the probe has stopped planting what it
        claims to, and every "not anywhere in it" above passes for nothing.
        (The session cookie and the Authorization header are not in the list:
        the SDK's own denylist removes those two even when asked for everything.)
        """
        assert PLANTED[planted] in json.dumps(events(captured["raw"]))


class TestProductionSettingsDecideWhetherItStarts:
    def test_with_no_dsn_the_sdk_is_not_initialised_and_not_even_imported(self):
        report = run_probe("settings", **PRODUCTION_ENV)

        assert report == {"sdk_imported": False, "module_imported": False}

    def test_with_a_dsn_it_starts_as_adr_029_describes(self):
        report = run_probe(
            "settings",
            **{
                **PRODUCTION_ENV,
                "SENTRY_DSN": DSN,
                "SENTRY_ENVIRONMENT": "staging",
                "FLY_IMAGE_REF": "registry.fly.io/pink-glove-staging:deployment-01JPROBE",
            },
        )

        assert report == {
            "sdk_imported": True,
            "module_imported": True,
            "active": True,
            "dsn": DSN,
            "environment": "staging",
            "release": "registry.fly.io/pink-glove-staging:deployment-01JPROBE",
            "send_default_pii": False,
            "max_request_body_size": "never",
            "include_local_variables": False,
            "traces_sample_rate": None,
            "auto_session_tracking": False,
            "send_client_reports": False,
            "trace_propagation_targets": [],
            "before_send": "before_send",
            # Django and Celery, named in `init`, and the SDK's defaults that
            # report or deduplicate. Nothing discovered from what is installed.
            "integrations": [
                "argv",
                "atexit",
                "celery",
                "dedupe",
                "django",
                "excepthook",
                "logging",
                "modules",
                "stdlib",
                "threading",
            ],
        }
