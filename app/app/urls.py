"""Root URL configuration."""

import re

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path, re_path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from app.spa import spa_index

urlpatterns = [
    path("health/", include("health.urls", namespace="health")),
    path("admin/", admin.site.urls),
    path("api/auth/", include("two_factor.urls", namespace="two_factor")),
    path("api/users/", include("users.urls", namespace="users")),
    path("api/organizations/", include("organizations.urls", namespace="organizations")),
    path("api/customers/", include("customers.urls", namespace="customers")),
    path("api/catalog/", include("catalog.urls", namespace="catalog")),
    path("api/audit/", include("audit.urls", namespace="audit")),
    path("api/scheduling/", include("scheduling.urls", namespace="scheduling")),
    path("api/billing/", include("billing.urls", namespace="billing")),
    # The schema and its browser both sit behind the global IsAuthenticated
    # default -- no AllowAny. See docs/DECISIONS.md ADR-019.
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="docs"),
]

# Static and media are served by whitenoise / the storage backend in every
# deployed environment; this is a local-development convenience only.
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)

# The built SPA, served from this origin (ADR-027). Last, and fenced off from
# every top-level prefix above it, so an unknown /api/ path is still a 404
# rather than a 200 with a page in it. The fence is derived from the patterns
# themselves plus STATIC_URL and MEDIA_URL, so a new top-level route (4b's
# webhooks, say) is excluded the moment it is added; a test checks every one.
# Whitenoise answers for the hashed assets before the request reaches Django
# at all; this only sees the routes the SPA's own router owns. Plain Django
# view, deliberately public: the login page is here.


def _top_level_prefixes(patterns):
    for pattern in patterns:
        head = str(pattern.pattern).strip("/").split("/", 1)[0]
        if head:
            yield head


#: The first path segment of every route above, plus static and media. A
#: request for one of these -- with or without a trailing slash, `/api` as
#: much as `/api/x` -- is never answered with the SPA.
SPA_EXCLUDED_PREFIXES = sorted(
    {
        *_top_level_prefixes(urlpatterns),
        settings.STATIC_URL.strip("/"),
        settings.MEDIA_URL.strip("/"),
    }
)

urlpatterns += [
    re_path(
        "^(?!(?:" + "|".join(map(re.escape, SPA_EXCLUDED_PREFIXES)) + ")(?:/|$)).*$",
        spa_index,
        name="spa",
    ),
]
