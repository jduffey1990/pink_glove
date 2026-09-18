"""
The built frontend is served from the API's origin (ADR-027).

The catch-all is the last URL pattern, and the thing that must never happen is
for it to swallow a miss on one of the real prefixes: an unknown /api/ path
answered with a 200 and a page of HTML would read, to the frontend, as a
successful call with an unparseable body.
"""

import pytest
from django.urls import reverse

INDEX = b"<!DOCTYPE html><html><body><div id=app></div></body></html>"


@pytest.fixture
def dist(tmp_path, settings):
    """A Vite-shaped build directory, wired in as UI_DIST_DIR."""
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "index-DGeD55ZR.js").write_bytes(b"console.log(1)")
    (tmp_path / "index.html").write_bytes(INDEX)
    settings.UI_DIST_DIR = str(tmp_path)
    return tmp_path


class TestSpaIndex:
    @pytest.mark.parametrize("path", ["/", "/schedule", "/jobs/abc-123", "/billing/settings"])
    def test_every_app_route_gets_the_index(self, api_client, dist, path):
        response = api_client.get(path)

        assert response.status_code == 200
        assert response["Content-Type"].startswith("text/html")
        assert response.content == INDEX

    def test_is_public(self, api_client, dist):
        """The login page lives here; nobody is signed in yet when they load it."""
        assert api_client.get("/login").status_code == 200

    def test_the_document_is_revalidated_on_every_load(self, api_client, dist):
        """
        The assets it names are cached for a year, so the document itself must
        not be, or a browser keeps loading last week's build after a deploy.
        """
        response = api_client.get("/schedule")

        assert response["Cache-Control"] == "no-cache"

    def test_head_is_answered_for_monitors(self, api_client, dist):
        response = api_client.head("/schedule")

        assert response.status_code == 200
        assert response["Cache-Control"] == "no-cache"

    def test_only_get(self, api_client, dist):
        assert api_client.post("/schedule").status_code == 405

    def test_reverse_name(self, dist):
        assert reverse("spa") == "/"


class TestSpaDoesNotShadowTheApi:
    @pytest.mark.parametrize(
        "path",
        [
            "/api/no-such-thing/",
            "/api/billing/no-such-thing/",
            "/admin/no-such-thing/",
            "/health/no-such-thing/",
            "/static/no-such-thing.css",
            "/media/no-such-thing.jpg",
            # The bare prefix, with and without its slash, is not a page either.
            "/api",
            "/api/",
            "/admin",
            "/health",
            "/static",
            "/media",
        ],
    )
    def test_a_miss_under_a_real_prefix_never_gets_the_page(self, api_client, dist, path):
        response = api_client.get(path)

        # The admin answers its own misses with a redirect to its login and
        # APPEND_SLASH redirects a bare prefix; the rest are plain 404s. What
        # matters is that none of them is the SPA.
        assert response.status_code in (301, 302, 404)
        assert response.content != INDEX

    @pytest.mark.parametrize("path", ["/apifoo", "/administrivia", "/healthy-habits"])
    def test_a_route_that_merely_starts_like_a_prefix_is_a_page(self, api_client, dist, path):
        """The fence is on path segments, not string prefixes."""
        assert api_client.get(path).content == INDEX

    def test_every_top_level_route_is_fenced_off(self, dist):
        """
        The fence is derived from urlpatterns, so a new top-level route (4b's
        webhooks, say) is excluded the moment it is added. This is the check
        that the derivation covers every one of them.
        """
        from django.urls import Resolver404, get_resolver, resolve

        from app.spa import spa_index

        for pattern in get_resolver().url_patterns:
            if getattr(pattern, "name", None) == "spa":
                continue  # the catch-all itself
            head = str(pattern.pattern).strip("/").split("/", 1)[0]
            for path in (f"/{head}", f"/{head}/", f"/{head}/no-such-thing/"):
                try:
                    match = resolve(path)
                except Resolver404:
                    continue
                assert match.func is not spa_index, f"{path} reached the SPA"

    @pytest.mark.django_db
    def test_the_schema_and_its_browser_are_not_pages(self, api_client, dist):
        for name in ("schema", "docs"):
            response = api_client.get(reverse(name))

            assert response.status_code == 403
            assert response.content != INDEX

    @pytest.mark.django_db
    def test_the_api_still_answers_as_json(self, api_client, dist):
        response = api_client.get(reverse("health:live"))

        assert response.status_code == 200
        assert response["Content-Type"].startswith("application/json")


class TestWithoutABuild:
    def test_says_what_is_missing_rather_than_500ing(self, api_client, settings):
        settings.UI_DIST_DIR = ""

        response = api_client.get("/schedule")

        assert response.status_code == 404
        assert b"UI_DIST_DIR" in response.content

    def test_a_directory_without_an_index_is_the_same(self, api_client, settings, tmp_path):
        settings.UI_DIST_DIR = str(tmp_path)

        assert api_client.get("/schedule").status_code == 404

    def test_a_miss_is_not_remembered(self, api_client, settings, tmp_path):
        """A build that appears later, or a transient read error, must not pin
        a 404 for the life of the process."""
        settings.UI_DIST_DIR = str(tmp_path)
        assert api_client.get("/schedule").status_code == 404

        (tmp_path / "index.html").write_bytes(INDEX)

        assert api_client.get("/schedule").content == INDEX


class TestImmutableAssets:
    """
    Whitenoise caches forever only what it can prove is content-hashed. It
    knows Django's shape; the middleware subclass teaches it Vite's.
    """

    @pytest.fixture
    def middleware(self, dist, settings, tmp_path):
        from app.middleware.static_files import WhiteNoiseMiddleware

        (tmp_path / "static-root").mkdir()
        settings.STATIC_ROOT = str(tmp_path / "static-root")
        settings.WHITENOISE_ROOT = str(dist)
        return WhiteNoiseMiddleware(lambda request: None)

    def test_a_vite_hashed_asset_is_immutable(self, middleware):
        assert middleware.immutable_file_test("", "/assets/index-DGeD55ZR.js")
        assert middleware.immutable_file_test("", "/assets/JobDetailPage-DGm41TsS.css")

    def test_unhashed_files_are_not(self, middleware):
        assert not middleware.immutable_file_test("", "/favicon.ico")
        assert not middleware.immutable_file_test("", "/layers.css")
        assert not middleware.immutable_file_test("", "/index.html")

    @pytest.mark.parametrize(
        "url",
        [
            "/assets/some-file.js",  # a dash in the name is not a hash
            "/assets/my-component-x.js",
            "/assets/index-DGeD55Z.js",  # seven characters
            "/assets/index-DGeD55ZR1.js",  # nine
            "/assets/a-b+c=d1234.js",  # not base64url
        ],
    )
    def test_only_vites_exact_hash_shape_counts(self, middleware, url):
        assert not middleware.immutable_file_test("", url)

    def test_the_built_file_is_served_at_the_root(self, api_client, middleware, dist):
        """WHITENOISE_ROOT puts the build at /, not under /static/."""
        response = middleware(_request("/assets/index-DGeD55ZR.js"))

        assert response is not None
        assert response.status_code == 200
        assert "immutable" in response["Cache-Control"]

    def test_the_index_is_never_served_as_a_file(self, middleware, dist):
        """
        `/index.html` falls through to Django and app.spa, which sets
        no-cache and runs the middleware below whitenoise. Served as a file it
        would be cached for a minute and carry no X-Frame-Options.
        """
        assert middleware(_request("/index.html")) is None

    def test_a_served_file_still_gets_the_clickjacking_header(self, dist, settings, tmp_path):
        """XFrameOptionsMiddleware sits above whitenoise so its response hook
        runs on the files whitenoise answers."""
        from django.middleware.clickjacking import XFrameOptionsMiddleware

        from app.middleware.static_files import WhiteNoiseMiddleware

        (tmp_path / "static-root").mkdir(exist_ok=True)
        settings.STATIC_ROOT = str(tmp_path / "static-root")
        settings.WHITENOISE_ROOT = str(dist)
        chain = XFrameOptionsMiddleware(WhiteNoiseMiddleware(lambda request: None))
        names = [m.rsplit(".", 1)[-1] for m in settings.MIDDLEWARE]
        base_names = [m.rsplit(".", 1)[-1] for m in _base_middleware()]

        response = chain(_request("/assets/index-DGeD55ZR.js"))

        assert response["X-Frame-Options"] == "DENY"
        assert "WhiteNoiseMiddleware" not in names, "test settings strip whitenoise"
        assert base_names.index("XFrameOptionsMiddleware") < base_names.index(
            "WhiteNoiseMiddleware"
        )


def _request(path):
    from django.test import RequestFactory

    return RequestFactory().get(path)


def _base_middleware():
    from app.settings import base

    return base.MIDDLEWARE
