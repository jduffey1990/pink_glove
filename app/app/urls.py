"""Root URL configuration."""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("health/", include("health.urls", namespace="health")),
    path("admin/", admin.site.urls),
    path("api/auth/", include("two_factor.urls", namespace="two_factor")),
    path("api/users/", include("users.urls", namespace="users")),
    path("api/organizations/", include("organizations.urls", namespace="organizations")),
    # Phase 2: api/customers/, api/catalog/
    # Phase 3: api/scheduling/
    # Phase 4: api/billing/
]

# Static and media are served by whitenoise / the storage backend in every
# deployed environment; this is a local-development convenience only.
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
