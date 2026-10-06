from django.conf import settings
from django.middleware.csrf import get_token
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import (
    csrf_protect,
    ensure_csrf_cookie,
)

from rest_framework import (
    generics,
    permissions,
    status,
)
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.serializers import (
    TokenRefreshSerializer,
)
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.serializers import (
    LoginSerializer,
    UserProfileSerializer,
)


def set_refresh_cookie(response, refresh_token):
    response.set_cookie(
        key=settings.JWT_REFRESH_COOKIE_NAME,
        value=refresh_token,
        max_age=int(settings.JWT_REFRESH_COOKIE_MAX_AGE),
        httponly=True,
        secure=settings.JWT_REFRESH_COOKIE_SECURE,
        samesite=settings.JWT_REFRESH_COOKIE_SAMESITE,
        path=settings.JWT_REFRESH_COOKIE_PATH,
    )


def delete_refresh_cookie(response):
    response.delete_cookie(
        key=settings.JWT_REFRESH_COOKIE_NAME,
        path=settings.JWT_REFRESH_COOKIE_PATH,
        samesite=settings.JWT_REFRESH_COOKIE_SAMESITE,
    )


@method_decorator(
    ensure_csrf_cookie,
    name="dispatch",
)
class CSRFTokenAPIView(APIView):
    permission_classes = [
        permissions.AllowAny,
    ]
    authentication_classes = []

    def get(self, request):
        return Response(
            {
                "csrfToken": get_token(request),
            },
            status=status.HTTP_200_OK,
        )


@method_decorator(
    csrf_protect,
    name="dispatch",
)
class LoginAPIView(APIView):
    permission_classes = [
        permissions.AllowAny,
    ]
    authentication_classes = []

    def post(self, request):
        serializer = LoginSerializer(
            data=request.data,
            context={
                "request": request,
            },
        )

        serializer.is_valid(
            raise_exception=True,
        )

        result = dict(serializer.validated_data)

        refresh_token = result.pop("_refresh_cookie")

        response = Response(
            result,
            status=status.HTTP_200_OK,
        )

        set_refresh_cookie(
            response,
            refresh_token,
        )

        return response


@method_decorator(
    csrf_protect,
    name="dispatch",
)
class RefreshCookieAPIView(APIView):
    permission_classes = [
        permissions.AllowAny,
    ]
    authentication_classes = []

    def post(self, request):
        refresh_token = request.COOKIES.get(settings.JWT_REFRESH_COOKIE_NAME)

        if not refresh_token:
            return Response(
                {"detail": ("Refresh session is not available.")},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        serializer = TokenRefreshSerializer(
            data={
                "refresh": refresh_token,
            }
        )

        try:
            serializer.is_valid(raise_exception=True)
        except Exception:
            response = Response(
                {"detail": ("Refresh session is invalid " "or expired.")},
                status=status.HTTP_401_UNAUTHORIZED,
            )

            delete_refresh_cookie(response)

            return response

        result = dict(serializer.validated_data)

        rotated_refresh = result.pop(
            "refresh",
            None,
        )

        # Never expose refresh credentials to JS.
        result.pop(
            "refresh_token",
            None,
        )

        response = Response(
            result,
            status=status.HTTP_200_OK,
        )

        if rotated_refresh:
            set_refresh_cookie(
                response,
                rotated_refresh,
            )

        return response


@method_decorator(
    csrf_protect,
    name="dispatch",
)
class LogoutAPIView(APIView):
    permission_classes = [
        permissions.AllowAny,
    ]
    authentication_classes = []

    def post(self, request):
        refresh_token = request.COOKIES.get(settings.JWT_REFRESH_COOKIE_NAME)

        if refresh_token:
            try:
                token = RefreshToken(refresh_token)

                token.blacklist()

            except Exception:
                # Logout must remain idempotent.
                pass

        response = Response(
            {"detail": ("Logged out successfully.")},
            status=status.HTTP_200_OK,
        )

        delete_refresh_cookie(response)

        return response


class ProfileAPIView(generics.RetrieveUpdateAPIView):
    serializer_class = UserProfileSerializer

    permission_classes = [
        permissions.IsAuthenticated,
    ]

    def get_object(self):
        return self.request.user
