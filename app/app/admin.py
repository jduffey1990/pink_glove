"""
The admin site, behind the same second factor as everything else.

Stock Django admin takes an email and a password and hands back the very
session the API uses. That made `/admin/login/` a way round ADR-008: no code,
no throttle, and for a superuser a session that can act in any tenant. So this
site has no login form of its own. It admits only a session that has cleared a
2FA challenge (`VerifyView` marks it), and sends everyone else to sign in
through the app.
"""

from django.conf import settings
from django.contrib import admin
from django.contrib.admin.apps import AdminConfig
from django.shortcuts import redirect

#: Set on the session by `two_factor.views.VerifyView`, and by nothing else. A
#: sign-in that skipped the challenge (a cleaner's trusted device, a customer's
#: magic link) is a perfectly good session and still not one the admin accepts.
TWO_FACTOR_VERIFIED_SESSION_KEY = "two_factor_verified"


class TwoFactorAdminSite(admin.AdminSite):
    def has_permission(self, request):
        return super().has_permission(request) and bool(
            request.session.get(TWO_FACTOR_VERIFIED_SESSION_KEY)
        )

    def login(self, request, extra_context=None):
        if request.method == "GET" and self.has_permission(request):
            return redirect("admin:index")
        # GET or POST alike: there is no password form here to post to.
        return redirect(f"{settings.FRONTEND_BASE_URL}/login")


class PinkGloveAdminConfig(AdminConfig):
    default_site = "app.admin.TwoFactorAdminSite"
