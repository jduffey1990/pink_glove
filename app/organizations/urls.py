from django.urls import path

from organizations.views import CurrentOrganizationView

app_name = "organizations"

urlpatterns = [
    path("current/", CurrentOrganizationView.as_view(), name="current"),
]
