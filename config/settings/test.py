from .base import *  # noqa: F403,F401


DEBUG = False

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]

# Tests should not depend on production HTTPS behaviour.
SECURE_SSL_REDIRECT = False

# Keep test email isolated from real delivery.
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
