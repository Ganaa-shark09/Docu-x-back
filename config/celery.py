import os

from celery import Celery
from django.core.exceptions import ImproperlyConfigured


if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    raise ImproperlyConfigured(
        "DJANGO_SETTINGS_MODULE must be explicitly configured before Celery starts."
    )


app = Celery("docux")

app.config_from_object(
    "django.conf:settings",
    namespace="CELERY",
)

app.autodiscover_tasks()
