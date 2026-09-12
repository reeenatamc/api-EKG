"""Auth routes.

One path per method on the app's ``AuthService``, named after the method so the adapter
reads as a transcription rather than a translation.
"""

from __future__ import annotations

from django.urls import path

from accounts import views

urlpatterns = [
    path("register/", views.register, name="register"),
    path("verify/", views.verify_code, name="verify-code"),
    path("sign-in/", views.sign_in, name="sign-in"),
    path("password-reset/", views.request_password_reset, name="password-reset"),
    path("sign-out/", views.sign_out, name="sign-out"),
    path("session/", views.current_session, name="current-session"),
    path("account/", views.delete_account, name="delete-account"),
]
