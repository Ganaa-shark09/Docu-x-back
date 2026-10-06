import os

from django.core.exceptions import ImproperlyConfigured
from django.core.asgi import get_asgi_application


if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    raise ImproperlyConfigured("DJANGO_SETTINGS_MODULE must be explicitly configured.")


application = get_asgi_application()
