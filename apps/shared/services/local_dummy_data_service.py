"""Opt-in local-db adapter for the existing, service-backed DOCU-X demo seed.

This DOES NOT weaken seed_demo's isolated-database safety guard. Only the
new generate_dummy_data command uses this adapter.
"""

import os
from django.conf import settings
from django.core.management.base import CommandError
from django.db import connection

from apps.shared.services.demo_data_service import DemoDataService


class LocalDummyDataService(DemoDataService):
    @staticmethod
    def guard():
        if (
            not settings.DEBUG
            or settings.SETTINGS_MODULE != "config.settings.local"
            or os.environ.get("DJANGO_SETTINGS_MODULE") != "config.settings.local"
            or os.environ.get("DOCUX_LOCAL_DUMMY_DATA") != "YES"
            or os.environ.get("DOCUX_DEMO_ENABLED") != "YES"
        ):
            raise CommandError(
                "Local dummy data is disabled. Require DEBUG=True, local settings "
                "and DOCUX_LOCAL_DUMMY_DATA=YES and DOCUX_DEMO_ENABLED=YES."
            )
        db = connection.settings_dict
        if str(db.get("NAME")) != "docux" or str(db.get("HOST")) not in {
            "postgres",
            "docux_postgres",
            "localhost",
            "127.0.0.1",
        }:
            raise CommandError(
                "Refusing to seed: expected database name 'docux' on a local "
                "PostgreSQL host. No other database is permitted."
            )
