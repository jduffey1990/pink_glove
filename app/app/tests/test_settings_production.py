"""
The production settings module refuses to start misconfigured.

Every check here is one that would otherwise surface as a bad deploy: uploads
written to a disk the next deploy throws away, a blank page because the image
was built from the wrong directory, a cookie loosened to SameSite=None with
nothing cross-origin to justify it. The module is re-imported under a
controlled environment; nothing here touches the settings the suite runs on.
"""

import importlib
import os
import sys
from datetime import timedelta
from pathlib import Path

import decouple
import pytest
from django.core.exceptions import ImproperlyConfigured

#: production.py checks the key is present; the Fernet itself is built on first
#: use (base.fields), so a placeholder that is plainly not a key will do here.
ENCRYPTION_KEY = "not-a-real-key-presence-is-all-production-checks"

#: Everything production.py and base.py read that the assertions depend on.
#: Set explicitly so a developer's app/.env cannot change a test's outcome.
_BASELINE = {
    "SECRET_KEY": "x" * 64,
    "ALLOWED_HOSTS": "example.com",
    "FIELD_ENCRYPTION_KEY": ENCRYPTION_KEY,
    "FRONTEND_BASE_URL": "https://example.com",
    "CORS_ALLOWED_ORIGINS": "",
    "MEDIA_BACKEND": "s3",
    "MEDIA_BUCKET": "pink-glove-media",
    "BUCKET_NAME": "",
    "AWS_ENDPOINT_URL_S3": "https://fly.storage.tigris.dev",
    "AWS_REGION": "auto",
    "AWS_ACCESS_KEY_ID": "tid_test",
    "AWS_SECRET_ACCESS_KEY": "tsec_test",
    "NUM_PROXIES": "1",
}


@pytest.fixture
def load(monkeypatch, tmp_path):
    """
    Import app.settings.production under a given environment.

    Reloads base.py too, because production.py takes its values from there at
    import time. Afterwards base.py is reloaded once more under the real
    environment so the module in sys.modules is not left describing a
    fictional deployment.

    python-decouple reads os.environ first and then a .env file it finds by
    searching up from the settings package -- a developer's app/.env. That
    file is replaced with an empty repository for every load here, so a value
    a test leaves unset is genuinely unset and the outcome cannot depend on
    what someone happens to have in their .env.
    """
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html></html>")

    def _load(*, unset=(), **overrides):
        env = {**_BASELINE, "UI_DIST_DIR": str(dist), **overrides}
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        for key in unset:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setattr(decouple.config, "config", decouple.Config(decouple.RepositoryEmpty()))
        importlib.reload(importlib.import_module("app.settings.base"))
        if "app.settings.production" in sys.modules:
            return importlib.reload(sys.modules["app.settings.production"])
        return importlib.import_module("app.settings.production")

    yield _load

    monkeypatch.undo()
    importlib.reload(importlib.import_module("app.settings.base"))


class TestRequiredValues:
    def test_a_complete_environment_loads(self, load):
        production = load()

        assert production.DEBUG is False
        assert production.LOCAL is False
        assert production.ALLOWED_HOSTS == ["example.com"]

    @pytest.mark.parametrize(
        "missing", ["SECRET_KEY", "ALLOWED_HOSTS", "FIELD_ENCRYPTION_KEY", "FRONTEND_BASE_URL"]
    )
    def test_a_missing_required_value_fails_at_import(self, load, missing):
        with pytest.raises(ImproperlyConfigured, match=missing):
            load(**{missing: ""})

    def test_the_frontend_url_must_be_https(self, load):
        """A customer's sign-in token travels in it, in an email."""
        with pytest.raises(ImproperlyConfigured, match="https"):
            load(FRONTEND_BASE_URL="http://example.com")

    def test_a_developers_dot_env_cannot_change_an_outcome(self, load, monkeypatch, tmp_path):
        """
        The fixture's own guarantee, checked: a .env that says SSL redirect on
        and a test that leaves it unset still sees production's default.
        """
        dotenv = tmp_path / ".env"
        dotenv.write_text("SECURE_SSL_REDIRECT=True\nNUM_PROXIES=7\n")
        monkeypatch.setattr(
            decouple.config, "config", decouple.Config(decouple.RepositoryEnv(str(dotenv)))
        )
        assert decouple.config("NUM_PROXIES", cast=int) == 7, "the .env is in force"

        production = load(unset=("SECURE_SSL_REDIRECT", "NUM_PROXIES"))

        assert production.SECURE_SSL_REDIRECT is False
        assert production.REST_FRAMEWORK["NUM_PROXIES"] == 1

    def test_cors_origins_are_no_longer_required(self, load):
        """Single-origin (ADR-027): nothing cross-origin calls the API."""
        load(CORS_ALLOWED_ORIGINS="")

    def test_a_wildcard_host_is_refused(self, load):
        with pytest.raises(ImproperlyConfigured, match="ALLOWED_HOSTS"):
            load(ALLOWED_HOSTS="example.com,*")


class TestCookies:
    def test_single_origin_keeps_cookies_lax(self, load):
        production = load(CORS_ALLOWED_ORIGINS="")

        assert production.SESSION_COOKIE_SAMESITE == "Lax"
        assert production.CSRF_COOKIE_SAMESITE == "Lax"

    def test_a_separately_hosted_frontend_loosens_them_to_none(self, load):
        production = load(CORS_ALLOWED_ORIGINS="https://app.example.com")

        assert production.SESSION_COOKIE_SAMESITE == "None"
        assert production.CSRF_COOKIE_SAMESITE == "None"
        assert production.CORS_ALLOWED_ORIGINS == ["https://app.example.com"]

    def test_cookies_are_secure_either_way(self, load):
        production = load()

        assert production.SESSION_COOKIE_SECURE is True
        assert production.CSRF_COOKIE_SECURE is True
        assert production.SESSION_COOKIE_HTTPONLY is True


class TestMedia:
    def test_filesystem_media_is_refused(self, load):
        """Nothing serves /media/ outside DEBUG, and the disk is ephemeral."""
        with pytest.raises(ImproperlyConfigured, match="filesystem"):
            load(MEDIA_BACKEND="filesystem")

    def test_filesystem_is_also_the_default_and_so_refused_when_unset(self, load):
        with pytest.raises(ImproperlyConfigured, match="filesystem"):
            load(unset=("MEDIA_BACKEND",))

    @pytest.mark.parametrize("backend", ["s3", "gcs"])
    def test_a_cloud_backend_without_a_bucket_is_refused(self, load, backend):
        with pytest.raises(ImproperlyConfigured, match="bucket"):
            load(MEDIA_BACKEND=backend, MEDIA_BUCKET="", BUCKET_NAME="")

    @pytest.mark.parametrize("backend", ["s3", "gcs"])
    def test_the_bucket_falls_back_to_the_name_fly_storage_sets(self, load, backend):
        production = load(MEDIA_BACKEND=backend, MEDIA_BUCKET="", BUCKET_NAME="from-fly")

        assert production.STORAGES["default"]["OPTIONS"]["bucket_name"] == "from-fly"

    def test_gcs_objects_are_private_and_read_through_expiring_urls(self, load):
        options = load(MEDIA_BACKEND="gcs", MEDIA_URL_TTL_SECONDS="120").STORAGES["default"][
            "OPTIONS"
        ]

        assert options["bucket_name"] == "pink-glove-media"
        # None, not "private": uniform bucket-level access rejects per-object ACLs.
        assert options["default_acl"] is None
        assert options["querystring_auth"] is True
        assert options["expiration"] == timedelta(seconds=120)
        assert options["file_overwrite"] is False

    def test_s3_objects_are_private_and_read_through_expiring_urls(self, load):
        options = load(MEDIA_URL_TTL_SECONDS="120").STORAGES["default"]["OPTIONS"]

        assert options["bucket_name"] == "pink-glove-media"
        assert options["endpoint_url"] == "https://fly.storage.tigris.dev"
        assert options["region_name"] == "auto"
        assert options["access_key"] == "tid_test"
        assert options["secret_key"] == "tsec_test"
        assert options["addressing_style"] == "path"
        assert options["default_acl"] == "private"
        assert options["querystring_auth"] is True
        assert options["querystring_expire"] == 120
        assert options["file_overwrite"] is False

    def test_an_unknown_backend_is_refused(self, load):
        with pytest.raises(ValueError, match="MEDIA_BACKEND"):
            load(MEDIA_BACKEND="dropbox")


class TestFrontendBuild:
    def test_a_present_build_is_served(self, load, tmp_path):
        production = load()

        assert Path(production.UI_DIST_DIR) == tmp_path / "dist"
        assert production.WHITENOISE_ROOT == str(tmp_path / "dist")

    def test_a_missing_build_fails_the_start(self, load, tmp_path):
        with pytest.raises(ImproperlyConfigured, match="UI_DIST_DIR"):
            load(UI_DIST_DIR=str(tmp_path / "nowhere"))

    def test_an_explicitly_empty_value_means_hosted_elsewhere(self, load):
        production = load(UI_DIST_DIR="")

        assert production.UI_DIST_DIR == ""
        assert production.WHITENOISE_ROOT is None


class TestEmail:
    def test_the_backend_is_read_from_the_environment(self, load):
        """So a rehearsal of the image can print mail instead of needing a key."""
        console = "django.core.mail.backends.console.EmailBackend"

        assert load(EMAIL_BACKEND=console).EMAIL_BACKEND == console

    def test_and_is_smtp_by_default(self, load):
        production = load(unset=("EMAIL_BACKEND",))

        assert production.EMAIL_BACKEND == "django.core.mail.backends.smtp.EmailBackend"


class TestHsts:
    def test_subdomains_and_preload_default_on_for_an_app_on_its_own_subdomain(self, load):
        production = load(unset=("SECURE_HSTS_INCLUDE_SUBDOMAINS", "SECURE_HSTS_PRELOAD"))

        assert production.SECURE_HSTS_INCLUDE_SUBDOMAINS is True
        assert production.SECURE_HSTS_PRELOAD is True

    def test_and_can_be_switched_off_for_an_apex_domain(self, load):
        production = load(SECURE_HSTS_INCLUDE_SUBDOMAINS="False", SECURE_HSTS_PRELOAD="False")

        assert production.SECURE_HSTS_INCLUDE_SUBDOMAINS is False
        assert production.SECURE_HSTS_PRELOAD is False


class TestProxies:
    def test_num_proxies_defaults_to_the_one_fly_proxy(self, load):
        production = load(unset=("NUM_PROXIES",))

        assert production.REST_FRAMEWORK["NUM_PROXIES"] == 1

    def test_num_proxies_is_read_from_the_environment(self, load):
        assert load(NUM_PROXIES="2").REST_FRAMEWORK["NUM_PROXIES"] == 2

    def test_ssl_redirect_stays_off_because_the_edge_redirects(self, load):
        production = load(unset=("SECURE_SSL_REDIRECT",))

        assert production.SECURE_SSL_REDIRECT is False
        assert production.SECURE_PROXY_SSL_HEADER == ("HTTP_X_FORWARDED_PROTO", "https")


def test_the_suite_itself_is_not_running_on_production_settings():
    assert os.environ["DJANGO_SETTINGS_MODULE"] == "app.settings.test"
