from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403,F401


DEBUG = False


def required_env(name):
    value = env(name, default="").strip()  # noqa: F405

    if not value:
        raise ImproperlyConfigured(
            f"{name} must be configured in the production environment."
        )

    return value


# ------------------------------------------------------------------
# Core security
# ------------------------------------------------------------------

SECRET_KEY = required_env("DJANGO_SECRET_KEY")

ALLOWED_HOSTS = env.list(  # noqa: F405
    "DJANGO_ALLOWED_HOSTS",
    default=[],
)

if not ALLOWED_HOSTS:
    raise ImproperlyConfigured(
        "DJANGO_ALLOWED_HOSTS must contain at least one production hostname."
    )


CORS_ALLOWED_ORIGINS = env.list(  # noqa: F405
    "CORS_ALLOWED_ORIGINS",
    default=[],
)

CSRF_TRUSTED_ORIGINS = env.list(  # noqa: F405
    "CSRF_TRUSTED_ORIGINS",
    default=[],
)

WEBSOCKET_ALLOWED_ORIGINS = env.list(  # noqa: F405
    "WEBSOCKET_ALLOWED_ORIGINS",
    default=CORS_ALLOWED_ORIGINS,
)


# ------------------------------------------------------------------
# HTTPS / browser security
# ------------------------------------------------------------------

SECURE_SSL_REDIRECT = env.bool(  # noqa: F405
    "DJANGO_SECURE_SSL_REDIRECT",
    default=True,
)

SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

SESSION_COOKIE_HTTPONLY = True

SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"

SECURE_CONTENT_TYPE_NOSNIFF = True

X_FRAME_OPTIONS = "DENY"

SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"

SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"


# ------------------------------------------------------------------
# Reverse proxy
# ------------------------------------------------------------------

SECURE_PROXY_SSL_HEADER = (
    "HTTP_X_FORWARDED_PROTO",
    "https",
)

USE_X_FORWARDED_HOST = env.bool(  # noqa: F405
    "DJANGO_USE_X_FORWARDED_HOST",
    default=False,
)


# ------------------------------------------------------------------
# HSTS
# ------------------------------------------------------------------

SECURE_HSTS_SECONDS = env.int(  # noqa: F405
    "DJANGO_SECURE_HSTS_SECONDS",
    default=31536000,
)

SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool(  # noqa: F405
    "DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS",
    default=True,
)

SECURE_HSTS_PRELOAD = env.bool(  # noqa: F405
    "DJANGO_SECURE_HSTS_PRELOAD",
    default=True,
)


# ------------------------------------------------------------------
# Production logging
# ------------------------------------------------------------------

DOCUX_AI_LOG_LEVEL = env(  # noqa: F405
    "DOCUX_AI_LOG_LEVEL",
    default="INFO",
)

DJANGO_LOG_LEVEL = env(  # noqa: F405
    "DJANGO_LOG_LEVEL",
    default="INFO",
)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": ("%(asctime)s %(levelname)s " "%(name)s %(message)s"),
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": DJANGO_LOG_LEVEL,
    },
    "loggers": {
        "django.security": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        "apps.ai_engine": {
            "handlers": ["console"],
            "level": DOCUX_AI_LOG_LEVEL,
            "propagate": False,
        },
        "apps.document_processing": {
            "handlers": ["console"],
            "level": DOCUX_AI_LOG_LEVEL,
            "propagate": False,
        },
        "apps.documents": {
            "handlers": ["console"],
            "level": DOCUX_AI_LOG_LEVEL,
            "propagate": False,
        },
        "apps.realtime": {
            "handlers": ["console"],
            "level": DOCUX_AI_LOG_LEVEL,
            "propagate": False,
        },
    },
}

# Refresh tokens are stored in an HttpOnly cookie.
# Production must never allow an environment value
# to downgrade this cookie to non-Secure transport.
JWT_REFRESH_COOKIE_SECURE = True
