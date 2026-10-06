from pathlib import Path

import environ

from datetime import timedelta

BASE_DIR = Path(__file__).resolve().parent.parent.parent


env = environ.Env(
    DJANGO_DEBUG=(bool, False),
)

SECRET_KEY = env("DJANGO_SECRET_KEY", default="local-dev-secret-key")

DEBUG = env.bool("DJANGO_DEBUG", default=False)

ALLOWED_HOSTS = env.list(
    "DJANGO_ALLOWED_HOSTS",
    default=["localhost", "127.0.0.1", "0.0.0.0"],
)

INSTALLED_APPS = [
    # Daphne must be before django.contrib.staticfiles so its ASGI runserver wins.
    "daphne",
    "channels",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party
    "rest_framework",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "django_filters",
    "drf_spectacular",
    "django_celery_beat",
    "django_celery_results",
    "pgvector.django",
    # Local apps
    "apps.accounts",
    "apps.tenants",
    "apps.workspaces",
    "apps.documents",
    "apps.document_processing",
    "apps.ai_engine",
    "apps.data_firewall",
    "apps.vector_search",
    "apps.workflows",
    "apps.approvals",
    "apps.audit",
    "apps.integrations",
    "apps.billing",
    "apps.notifications",
    "apps.realtime",
    "apps.shared",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.realtime_asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("POSTGRES_DB", default="docux"),
        "USER": env("POSTGRES_USER", default="docux"),
        "PASSWORD": env("POSTGRES_PASSWORD", default="docux_password"),
        "HOST": env("POSTGRES_HOST", default="localhost"),
        "PORT": env("POSTGRES_PORT", default="5432"),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "accounts.User"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
    ],
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(
        minutes=env.int(
            "JWT_ACCESS_TOKEN_MINUTES",
            default=20,
        )
    ),
    "REFRESH_TOKEN_LIFETIME": timedelta(
        days=env.int(
            "JWT_REFRESH_TOKEN_DAYS",
            default=7,
        )
    ),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

JWT_REFRESH_COOKIE_NAME = env(
    "JWT_REFRESH_COOKIE_NAME",
    default="docux_refresh",
)

JWT_REFRESH_COOKIE_PATH = env(
    "JWT_REFRESH_COOKIE_PATH",
    default="/api/auth/",
)

JWT_REFRESH_COOKIE_HTTPONLY = True

JWT_REFRESH_COOKIE_SECURE = env.bool(
    "JWT_REFRESH_COOKIE_SECURE",
    default=False,
)

JWT_REFRESH_COOKIE_SAMESITE = env(
    "JWT_REFRESH_COOKIE_SAMESITE",
    default="Lax",
)

JWT_REFRESH_COOKIE_MAX_AGE = SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"].total_seconds()

SPECTACULAR_SETTINGS = {
    "TITLE": "DocuX AI API",
    "DESCRIPTION": "Secure private AI workspace for confidential company documents.",
    "VERSION": "1.0.0",
}

CORS_ALLOWED_ORIGINS = env.list(
    "CORS_ALLOWED_ORIGINS",
    default=["http://localhost:4200", "http://127.0.0.1:4200"],
)

CORS_ALLOW_CREDENTIALS = True

CSRF_TRUSTED_ORIGINS = env.list(
    "CSRF_TRUSTED_ORIGINS",
    default=CORS_ALLOWED_ORIGINS,
)

WEBSOCKET_ALLOWED_ORIGINS = env.list(
    "WEBSOCKET_ALLOWED_ORIGINS",
    default=CORS_ALLOWED_ORIGINS,
)
WEBSOCKET_TICKET_MAX_AGE_SECONDS = env.int(
    "WEBSOCKET_TICKET_MAX_AGE_SECONDS",
    default=60,
)
WEBSOCKET_MAX_DOCUMENT_SUBSCRIPTIONS = env.int(
    "WEBSOCKET_MAX_DOCUMENT_SUBSCRIPTIONS",
    default=200,
)

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            # Keep realtime traffic isolated from Celery broker/result DBs.
            "hosts": [
                env(
                    "CHANNEL_LAYER_REDIS_URL",
                    default="redis://localhost:6379/2",
                )
            ],
            "capacity": env.int("CHANNEL_LAYER_CAPACITY", default=1000),
            "expiry": env.int("CHANNEL_LAYER_EXPIRY_SECONDS", default=60),
        },
    }
}

CELERY_BROKER_URL = env(
    "CELERY_BROKER_URL",
    default="redis://localhost:6379/0",
)
CELERY_RESULT_BACKEND = env(
    "CELERY_RESULT_BACKEND",
    default="redis://localhost:6379/1",
)
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE

DOCUMENT_PROCESSING_QUEUE = env(
    "DOCUMENT_PROCESSING_QUEUE",
    default="document_processing",
)
CELERY_TASK_ROUTES = {
    "apps.documents.tasks.document_processing_tasks.process_document_pipeline": {
        "queue": DOCUMENT_PROCESSING_QUEUE,
    },
}

# Document upload security
DOCUMENT_UPLOAD_MAX_FILE_SIZE_BYTES = env.int(
    "DOCUMENT_UPLOAD_MAX_FILE_SIZE_BYTES",
    default=50 * 1024 * 1024,
)

DOCUMENT_UPLOAD_MAX_ARCHIVE_UNCOMPRESSED_BYTES = env.int(
    "DOCUMENT_UPLOAD_MAX_ARCHIVE_UNCOMPRESSED_BYTES",
    default=200 * 1024 * 1024,
)

DOCUMENT_UPLOAD_MAX_ARCHIVE_ENTRIES = env.int(
    "DOCUMENT_UPLOAD_MAX_ARCHIVE_ENTRIES",
    default=5000,
)

DOCUMENT_UPLOAD_MAX_ARCHIVE_COMPRESSION_RATIO = env.int(
    "DOCUMENT_UPLOAD_MAX_ARCHIVE_COMPRESSION_RATIO",
    default=200,
)

DOCUMENT_UPLOAD_MAX_IMAGE_PIXELS = env.int(
    "DOCUMENT_UPLOAD_MAX_IMAGE_PIXELS",
    default=50_000_000,
)

DOCUMENT_UPLOAD_MAX_PDF_PAGES = env.int(
    "DOCUMENT_UPLOAD_MAX_PDF_PAGES",
    default=1000,
)
