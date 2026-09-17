"""
The OpenAPI schema is the frontend's contract (docs/DECISIONS.md ADR-019).

The zero-warning assertion is the load-bearing one. drf-spectacular emits a
warning rather than failing whenever it cannot work out an action's request or
response shape -- exactly the actions the frontend would then have to guess at.
Turning that into a test failure is what keeps `@extend_schema` from being
forgotten on the next `@action` somebody adds.
"""

import pytest
from django.urls import reverse
from drf_spectacular.drainage import GENERATOR_STATS
from drf_spectacular.generators import SchemaGenerator


def _generate():
    """
    Generate the schema, returning (schema, problems).

    Both caches are read. spectacular files "unable to guess serializer" under
    *error*, not warning, so checking `_warn_cache` alone passes vacuously
    over precisely the views that need annotating.
    """
    GENERATOR_STATS.reset()
    schema = SchemaGenerator().get_schema(request=None, public=True)
    return schema, sorted([*GENERATOR_STATS._warn_cache, *GENERATOR_STATS._error_cache])


class TestSchemaGeneration:
    def test_schema_generates_without_warnings(self):
        schema, problems = _generate()

        assert not problems, (
            "drf-spectacular could not describe part of the API:\n  "
            + "\n  ".join(problems)
            + "\n\nAnnotate the offending action with @extend_schema. An action "
            "spectacular cannot describe is one the frontend cannot call correctly."
        )

    def test_schema_covers_the_api(self):
        """Guards the guard: a schema with no paths would pass vacuously."""
        schema, _ = _generate()

        assert schema["paths"], "The generated schema contains no paths at all."
        assert schema["info"]["title"] == "pink_glove API"


def _routed_viewsets(patterns=None):
    from django.urls import URLPattern, URLResolver, get_resolver

    for pattern in get_resolver().url_patterns if patterns is None else patterns:
        if isinstance(pattern, URLResolver):
            yield from _routed_viewsets(pattern.url_patterns)
        elif isinstance(pattern, URLPattern):
            cls = getattr(pattern.callback, "cls", None)
            if cls is not None and hasattr(cls, "get_extra_actions"):
                yield cls


class TestEveryActionIsDescribed:
    """
    The zero-warning test has a blind spot. On a viewset, spectacular does not
    warn about an `@action` it cannot describe: it quietly borrows the
    viewset's `serializer_class` for both request and response. `reveal-access`
    was published as "takes a ServiceLocation, returns a ServiceLocation" that
    way, and the frontend hand-wrote the real shape beside the generated one.
    """

    def test_every_action_carries_extend_schema(self):
        undescribed = sorted(
            {
                f"{cls.__module__}.{cls.__name__}.{extra.__name__}"
                for cls in _routed_viewsets()
                for extra in cls.get_extra_actions()
                if "schema" not in extra.kwargs
            }
        )

        assert not undescribed, (
            "These actions have no @extend_schema, so their request and response "
            "in the generated schema are the viewset's serializer, not their own:\n  "
            + "\n  ".join(undescribed)
        )


@pytest.mark.django_db
class TestSchemaEndpoints:
    def test_schema_requires_authentication(self, api_client):
        assert api_client.get(reverse("schema")).status_code == 403

    def test_docs_require_authentication(self, api_client):
        assert api_client.get(reverse("docs")).status_code == 403

    def test_signed_in_user_can_fetch_the_schema(self, authed_client):
        response = authed_client.get(reverse("schema"))

        assert response.status_code == 200
        assert b"openapi" in response.content
