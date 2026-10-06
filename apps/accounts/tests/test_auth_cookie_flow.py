from django.conf import settings
from django.contrib.auth import get_user_model

from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from rest_framework_simplejwt.token_blacklist.models import (
    BlacklistedToken,
)
from rest_framework_simplejwt.tokens import RefreshToken


User = get_user_model()


class AuthCookieFlowTests(APITestCase):
    password = "Strong-Test-Password-123!"

    def setUp(self):
        self.user = User.objects.create_user(
            username="auth-test-user",
            email="auth-test@example.com",
            password=self.password,
        )

    def login_payload(self):
        return {
            "email_or_username": self.user.email,
            "password": self.password,
        }

    def login(self, client=None):
        client = client or self.client

        return client.post(
            "/api/auth/login/",
            self.login_payload(),
            format="json",
        )

    def test_login_returns_access_only_and_sets_refresh_cookie(self):
        response = self.login()

        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
        )

        self.assertIn(
            "access",
            response.data,
        )

        self.assertIn(
            "user",
            response.data,
        )

        self.assertNotIn(
            "refresh",
            response.data,
        )

        cookie_name = settings.JWT_REFRESH_COOKIE_NAME

        self.assertIn(
            cookie_name,
            response.cookies,
        )

        cookie = response.cookies[cookie_name]

        self.assertTrue(
            cookie["httponly"],
        )

        self.assertEqual(
            bool(cookie["secure"]),
            bool(settings.JWT_REFRESH_COOKIE_SECURE),
        )

        self.assertEqual(
            cookie["samesite"],
            settings.JWT_REFRESH_COOKIE_SAMESITE,
        )

        self.assertEqual(
            cookie["path"],
            settings.JWT_REFRESH_COOKIE_PATH,
        )

    def test_login_requires_valid_csrf_when_checks_are_enabled(self):
        client = APIClient(
            enforce_csrf_checks=True,
        )

        csrf_response = client.get(
            "/api/auth/csrf/",
        )

        self.assertEqual(
            csrf_response.status_code,
            status.HTTP_200_OK,
        )

        csrf_cookie = client.cookies[settings.CSRF_COOKIE_NAME]

        blocked = client.post(
            "/api/auth/login/",
            self.login_payload(),
            format="json",
        )

        self.assertEqual(
            blocked.status_code,
            status.HTTP_403_FORBIDDEN,
        )

        allowed = client.post(
            "/api/auth/login/",
            self.login_payload(),
            format="json",
            HTTP_X_CSRFTOKEN=csrf_cookie.value,
        )

        self.assertEqual(
            allowed.status_code,
            status.HTTP_200_OK,
        )

    def test_refresh_rotates_cookie_and_blacklists_old_token(self):
        login_response = self.login()

        self.assertEqual(
            login_response.status_code,
            status.HTTP_200_OK,
        )

        cookie_name = settings.JWT_REFRESH_COOKIE_NAME

        old_refresh = login_response.cookies[cookie_name].value

        old_jti = RefreshToken(old_refresh)["jti"]

        self.client.cookies[cookie_name] = old_refresh

        response = self.client.post(
            "/api/auth/refresh/",
            {},
            format="json",
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
        )

        self.assertIn(
            "access",
            response.data,
        )

        self.assertNotIn(
            "refresh",
            response.data,
        )

        self.assertIn(
            cookie_name,
            response.cookies,
        )

        new_refresh = response.cookies[cookie_name].value

        self.assertNotEqual(
            old_refresh,
            new_refresh,
        )

        self.assertTrue(
            BlacklistedToken.objects.filter(
                token__jti=old_jti,
            ).exists(),
        )

    def test_logout_blacklists_refresh_and_deletes_cookie(self):
        login_response = self.login()

        self.assertEqual(
            login_response.status_code,
            status.HTTP_200_OK,
        )

        access = login_response.data["access"]

        cookie_name = settings.JWT_REFRESH_COOKIE_NAME

        refresh = login_response.cookies[cookie_name].value

        refresh_jti = RefreshToken(refresh)["jti"]

        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {access}",
        )

        self.client.cookies[cookie_name] = refresh

        response = self.client.post(
            "/api/auth/logout/",
            {},
            format="json",
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
        )

        self.assertTrue(
            BlacklistedToken.objects.filter(
                token__jti=refresh_jti,
            ).exists(),
        )

        self.assertIn(
            cookie_name,
            response.cookies,
        )

        deleted_cookie = response.cookies[cookie_name]

        self.assertEqual(
            deleted_cookie.value,
            "",
        )

        self.assertEqual(
            int(deleted_cookie["max-age"]),
            0,
        )
