from django.urls import path

from apps.accounts.views import (
    CSRFTokenAPIView,
    LoginAPIView,
    LogoutAPIView,
    ProfileAPIView,
    RefreshCookieAPIView,
)


urlpatterns = [
    path(
        "csrf/",
        CSRFTokenAPIView.as_view(),
        name="auth-csrf",
    ),
    path(
        "login/",
        LoginAPIView.as_view(),
        name="auth-login",
    ),
    path(
        "refresh/",
        RefreshCookieAPIView.as_view(),
        name="auth-refresh",
    ),
    # Temporary backward-compatible alias.
    path(
        "token/refresh/",
        RefreshCookieAPIView.as_view(),
        name="auth-token-refresh-legacy",
    ),
    path(
        "logout/",
        LogoutAPIView.as_view(),
        name="auth-logout",
    ),
    path(
        "profile/",
        ProfileAPIView.as_view(),
        name="auth-profile",
    ),
]
