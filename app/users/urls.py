from django.urls import include, path
from rest_framework.routers import SimpleRouter

from users.views import (
    InvitationAcceptView,
    InvitationInspectView,
    LogoutView,
    MagicLinkConsumeView,
    MagicLinkRequestView,
    MembershipViewSet,
    SessionView,
)

app_name = "users"

router = SimpleRouter()
router.register("memberships", MembershipViewSet, basename="membership")

urlpatterns = [
    path("session/", SessionView.as_view(), name="session"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("magic-link/", MagicLinkRequestView.as_view(), name="magic-link-request"),
    path("magic-link/consume/", MagicLinkConsumeView.as_view(), name="magic-link-consume"),
    path("invitations/inspect/", InvitationInspectView.as_view(), name="invitation-inspect"),
    path("invitations/accept/", InvitationAcceptView.as_view(), name="invitation-accept"),
    path("", include(router.urls)),
]
