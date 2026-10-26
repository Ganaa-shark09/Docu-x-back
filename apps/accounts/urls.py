from django.urls import path
from rest_framework.routers import DefaultRouter
from apps.accounts.views.administration_views import (
    ManagedCompanyViewSet,
    ManagedUserViewSet,
)
from apps.accounts.views import (
    CSRFTokenAPIView,
    LoginAPIView,
    LogoutAPIView,
    ProfileAPIView,
    RefreshCookieAPIView,
)

router = DefaultRouter()
router.register("companies", ManagedCompanyViewSet, basename="managed-companies")
router.register("users", ManagedUserViewSet, basename="managed-users")


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
urlpatterns += router.urls
