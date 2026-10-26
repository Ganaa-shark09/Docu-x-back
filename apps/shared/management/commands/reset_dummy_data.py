"""Opt-in removal of only tracked, pristine synthetic demo data."""

import json
from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from apps.shared.services.local_dummy_data_service import LocalDummyDataService


class Command(BaseCommand):
    help = "Delete ONLY owned demo dataset; refuse after untracked changes."

    def add_arguments(self, parser):
        parser.add_argument("--confirm-reset", action="store_true")

    def handle(self, *args, **options):
        if not options["confirm_reset"]:
            raise CommandError("Reset requires --confirm-reset.")
        service = LocalDummyDataService()
        result = service.reset(confirmed=True)
        path = Path(settings.BASE_DIR) / ".docux_dummy_credentials.json"
        if path.is_file() and not path.is_symlink():
            path.unlink()
        self.stdout.write(json.dumps(result, sort_keys=True))
        self.stdout.write(self.style.SUCCESS("Tracked dummy data removed."))
