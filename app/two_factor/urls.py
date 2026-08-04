from django.urls import path

from two_factor.views import LoginView, ResendView, VerifyView

app_name = "two_factor"

urlpatterns = [
    path("login/", LoginView.as_view(), name="login"),
    path("verify/", VerifyView.as_view(), name="verify"),
    path("resend/", ResendView.as_view(), name="resend"),
]
