"""Verify owned demo records in the existing local PostgreSQL database."""

import json
from django.core.management.base import BaseCommand
from apps.shared.services.local_dummy_data_service import LocalDummyDataService


class Command(BaseCommand):
    help = "Verify real local synthetic DOCU-X records and security properties."

    def handle(self, *args, **options):
        service = LocalDummyDataService()
        result = service.verify(full=True)
        self.stdout.write(json.dumps(result, sort_keys=True, default=str))
        self.stdout.write(self.style.SUCCESS("DOCU-X dummy data verification passed."))
