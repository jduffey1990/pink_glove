from django.urls import include, path
from rest_framework.routers import SimpleRouter

app_name = "scheduling"

router = SimpleRouter()

urlpatterns = [path("", include(router.urls))]
