from django.urls import path

from two_factor.views import (
    LoginView,
    PasswordResetConfirmView,
    PasswordResetRequestView,
    ResendView,
    VerifyView,
)

app_name = "two_factor"

urlpatterns = [
    path("login/", LoginView.as_view(), name="login"),
    path("verify/", VerifyView.as_view(), name="verify"),
    path("resend/", ResendView.as_view(), name="resend"),
    path("password-reset/", PasswordResetRequestView.as_view(), name="password-reset"),
    path(
        "password-reset/confirm/",
        PasswordResetConfirmView.as_view(),
        name="password-reset-confirm",
    ),
]
