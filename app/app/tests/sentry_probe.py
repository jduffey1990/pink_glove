"""
A child interpreter for `test_sentry.py`. Not collected by pytest.

Sentry's Django integration patches Django itself, once per process and for
good. Doing that inside the suite would leave every later test running on a
patched framework, so the SDK is only ever started here, in a process that
exits. Nothing can be sent: the transport keeps envelopes in a list, and the
DSN's host is under `.invalid`, which never resolves.

    python -m app.tests.sentry_probe capture
    python -m app.tests.sentry_probe settings

`capture` makes a request to a view that raises, runs a Celery task that
raises and logs an ERROR, each with credentials planted where real ones would
be (`sentry_probe_app.py`), three times over:

    shipped    `app.sentry.init`, exactly as production calls it. First, so
               it runs in a process nothing else has configured.
    loosened   the SDK told to collect everything (bodies, locals, the
               user), with `app.sentry.before_send` as the only defence
    raw        the same with no defence at all, to prove the planted values
               are there to be seen when nothing removes them

`settings` imports production settings from the caller's environment, sets
Django up, and reports whether the SDK was imported and how it was configured.

Either prints one JSON document on a line starting with MARKER.
"""

import json
import os
import sys

DSN = "https://public@sentry.invalid/1"
MARKER = "PROBE-RESULT "

#: Each one distinct, so a test can say which of them got out.
PLANTED = {
    "body": "PLANTED-BODY-4821#",
    "local": "PLANTED-LOCAL-9930",
    "query": "PLANTED-QUERY-TOKEN",
    "cookie": "PLANTED-SESSION-COOKIE",
    "csrf": "PLANTED-CSRF",
    "authorization": "PLANTED-BEARER",
    "task": "PLANTED-TASK-KWARG",
    "log_extra": "PLANTED-LOG-EXTRA",
    # Bound to a query the view runs. The statement's text is kept as a
    # breadcrumb; the value bound to it must not be.
    "sql_param": "PLANTED-SQL-PARAMETER",
    # TEST-NET-3 (RFC 5737), in the header Fly's proxy adds to every request.
    "client_ip": "203.0.113.77",
    # The shape of a Django signed token (`billing.connect.pay_token`):
    # payload, timestamp and signature, far longer than any UUID.
    "path": "IlBMQU5URUQtUEFUSC10b2tlbi1wYXlsb2FkIg:1vPATH:PLANTED-PATH-signature-0123456789abcdef",
    "referer": "IlBMQU5URUQtUkVGLXRva2VuLXBheWxvYWQi:1vREFR:PLANTED-REFERER-signature-0123456789abc",
}


def _keeping_transport(envelopes: list):
    from sentry_sdk.transport import Transport

    class KeepingTransport(Transport):
        def capture_envelope(self, envelope):
            for item in envelope.items:
                envelopes.append({"type": item.type, "payload": json.loads(item.get_bytes())})

    return KeepingTransport


def _exercise() -> None:
    import logging

    from django.core.wsgi import get_wsgi_application
    from django.test import RequestFactory

    from app.tests.sentry_probe_app import boom_task

    # Through the WSGI application itself, as gunicorn calls it, and not the
    # test client: the client skips the WSGI layer, which is where the SDK
    # reads the URL, the query string and the headers from.
    environ = (
        RequestFactory()
        .post(
            f"/probe/{PLANTED['path']}/boom/?token={PLANTED['query']}&page=2",
            data=json.dumps({"gate_code": PLANTED["body"], "alarm_code": PLANTED["body"]}),
            content_type="application/json",
            headers={
                "Cookie": f"sessionid={PLANTED['cookie']}; csrftoken={PLANTED['csrf']}",
                "Authorization": f"Bearer {PLANTED['authorization']}",
                "X-CSRFToken": PLANTED["csrf"],
                "Referer": f"https://testserver/pay/{PLANTED['referer']}",
                "Fly-Client-IP": PLANTED["client_ip"],
            },
        )
        .environ
    )
    statuses = []
    body = get_wsgi_application()(environ, lambda status, headers: statuses.append(status))
    b"".join(body)
    assert statuses == ["500 Internal Server Error"], statuses

    boom_task.apply(
        args=["4f6c1f0e-63a5-4f0e-9d53-1f0a3c1f9b77"], kwargs={"gate_code": PLANTED["task"]}
    )
    logging.getLogger("app.tests.sentry_probe").error(
        "probe log line", extra={"alarm_code": PLANTED["log_extra"]}
    )


def _capture() -> dict:
    os.environ["DJANGO_SETTINGS_MODULE"] = "app.settings.test"

    import django
    from django.conf import settings

    settings.ROOT_URLCONF = "app.tests.sentry_probe_app"
    # The planted session cookie must not send the session middleware to a
    # database this process does not have.
    settings.SESSION_ENGINE = "django.contrib.sessions.backends.signed_cookies"
    # Nor has it the suite's Postgres. The one query the view runs needs no
    # table, so an empty in-memory database will do.
    settings.DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
    settings.CELERY_TASK_EAGER_PROPAGATES = False
    django.setup()

    import sentry_sdk
    from sentry_sdk.integrations.celery import CeleryIntegration
    from sentry_sdk.integrations.django import DjangoIntegration

    from app import sentry

    def run(start) -> list:
        envelopes: list = []
        start(_keeping_transport(envelopes))
        _exercise()
        sentry_sdk.flush()
        return envelopes

    def collect_everything(transport, before_send):
        sentry_sdk.init(
            dsn=DSN,
            environment="probe",
            release="probe-release",
            transport=transport,
            before_send=before_send,
            integrations=[DjangoIntegration(), CeleryIntegration()],
            send_default_pii=True,
            max_request_body_size="always",
            include_local_variables=True,
        )

    return {
        "shipped": run(
            lambda transport: sentry.init(
                dsn=DSN, environment="probe", release="probe-release", transport=transport
            )
        ),
        "loosened": run(lambda transport: collect_everything(transport, sentry.before_send)),
        "raw": run(lambda transport: collect_everything(transport, None)),
    }


def _settings() -> dict:
    os.environ["DJANGO_SETTINGS_MODULE"] = "app.settings.production"

    import django

    django.setup()

    # Everything a web or worker process has imported by its first request:
    # the URLconf pulls in every view, the Celery app every task.
    from django.urls import get_resolver

    from app.celery import celery_app

    get_resolver().url_patterns  # noqa: B018
    celery_app.loader.import_default_modules()

    report = {
        "sdk_imported": "sentry_sdk" in sys.modules,
        "module_imported": "app.sentry" in sys.modules,
    }
    if report["sdk_imported"]:
        import sentry_sdk

        client = sentry_sdk.get_client()
        options = client.options
        report.update(
            active=client.is_active(),
            dsn=options["dsn"],
            environment=options["environment"],
            release=options["release"],
            send_default_pii=options["send_default_pii"],
            max_request_body_size=options["max_request_body_size"],
            include_local_variables=options["include_local_variables"],
            traces_sample_rate=options["traces_sample_rate"],
            auto_session_tracking=options["auto_session_tracking"],
            send_client_reports=options["send_client_reports"],
            trace_propagation_targets=list(options["trace_propagation_targets"]),
            before_send=getattr(options["before_send"], "__qualname__", None),
            integrations=sorted(client.integrations),
        )
    return report


if __name__ == "__main__":
    result = {"capture": _capture, "settings": _settings}[sys.argv[1]]()
    sys.stdout.write(MARKER + json.dumps(result) + "\n")
