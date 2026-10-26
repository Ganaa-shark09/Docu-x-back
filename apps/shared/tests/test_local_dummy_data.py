"""Security regression tests for the opt-in local dummy-data entry point."""

from unittest.mock import patch
import pytest
from django.core.management.base import CommandError
from django.test import override_settings
from apps.shared.services.local_dummy_data_service import LocalDummyDataService


def test_default_command_guard_refuses(monkeypatch):
    monkeypatch.delenv("DOCUX_LOCAL_DUMMY_DATA", raising=False)
    with pytest.raises(CommandError):
        LocalDummyDataService.guard()


def test_local_guard_requires_exact_database_and_host(monkeypatch):
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "config.settings.local")
    monkeypatch.setenv("DOCUX_LOCAL_DUMMY_DATA", "YES")
    monkeypatch.setenv("DOCUX_DEMO_ENABLED", "YES")
    with override_settings(DEBUG=True, SETTINGS_MODULE="config.settings.local"):
        with patch("apps.shared.services.local_dummy_data_service.connection") as db:
            for name, host in [
                ("docux_demo_sandbox", "postgres"),
                ("docux", "remote.example.com"),
            ]:
                db.settings_dict = {"NAME": name, "HOST": host}
                with pytest.raises(CommandError):
                    LocalDummyDataService.guard()
            db.settings_dict = {"NAME": "docux", "HOST": "postgres"}
            LocalDummyDataService.guard()
