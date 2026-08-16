from .base import *  # noqa: F403,F401

DEBUG = True


# External AI timing/routing logs are intentionally enabled in local development.
# This keeps Docker logs useful for model-selection and latency profiling without
# increasing framework-wide logging noise.
DOCUX_AI_LOG_LEVEL = env("DOCUX_AI_LOG_LEVEL", default="INFO")  # noqa: F405

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
        },
    },
    "loggers": {
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
