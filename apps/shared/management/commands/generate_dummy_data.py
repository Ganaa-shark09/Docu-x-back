"""Populate the existing local `docux` DB with full synthetic demo tenant data.

The importer is DemoDataService.seed(full=True): existing domain services, not
model-state fabrication. This wrapper generates a local password and ensures
that background Celery workers cannot race synchronous demonstration steps.
"""

import json
import os
import secrets
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.shared.models import DemoDataset
from apps.shared.services.local_dummy_data_service import LocalDummyDataService

CREDENTIAL_FILENAME = ".docux_dummy_credentials.json"


def _credentials_path():
    return Path(settings.BASE_DIR) / CREDENTIAL_FILENAME


def _load_or_create_password():
    path = _credentials_path()
    if path.exists():
        if path.is_symlink():
            raise CommandError("Credential file cannot be a symlink.")
        try:
            content = json.loads(path.read_text(encoding="utf-8"))
            if (
                content.get("username") != "docux_demo_admin"
                or len(content.get("password", "")) < 12
            ):
                raise ValueError("Invalid local dummy-data credentials")
            return content["password"], False
        except (ValueError, OSError, TypeError) as exc:
            raise CommandError(
                "Invalid demo credential file; refusing to overwrite it."
            ) from exc
    if DemoDataset.objects.filter(key=LocalDummyDataService.KEY).exists():
        raise CommandError(
            "Demo data already exists but its credentials file is missing. "
            "Do not regenerate a different password: restore the credentials "
            "file or reset the demo account password explicitly."
        )
    password = secrets.token_urlsafe(32)
    content = {
        "username": "docux_demo_admin",
        "password": password,
        "database": "docux",
        "note": "LOCAL SYNTHETIC DATA ONLY",
    }
    # Exclusive creation avoids replacing previously created credentials.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(str(path), flags, 0o600)
    except FileExistsError as exc:
        raise CommandError(
            "Credentials were concurrently created. Retry the command."
        ) from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(content, stream, indent=2)
            stream.write("\n")
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return password, True


class Command(BaseCommand):
    help = (
        "Seed real linked synthetic Feature 1-10 data into LOCAL PostgreSQL docux only."
    )

    def handle(self, *args, **options):
        service = LocalDummyDataService()
        service.guard()  # refuse before creating any password or other files
        password, created = _load_or_create_password()
        previous = os.environ.get("DOCUX_DEMO_PASSWORD")
        os.environ["DOCUX_DEMO_PASSWORD"] = password
        try:
            # Existing demo tests patch enqueue for the same reason: seed stages
            # advance domain workflow runs synchronously. A real Celery worker
            # must not race the uncommitted transaction.
            with patch(
                "apps.workflows.services.workflow_engine_service."
                "WorkflowEngineService.enqueue"
            ):
                report = service.seed(full=True)
                verified = service.verify(full=True)
        except Exception:
            if (
                created
                and not DemoDataset.objects.filter(
                    key=LocalDummyDataService.KEY
                ).exists()
            ):
                _credentials_path().unlink(missing_ok=True)
            raise
        finally:
            if previous is None:
                os.environ.pop("DOCUX_DEMO_PASSWORD", None)
            else:
                os.environ["DOCUX_DEMO_PASSWORD"] = previous
        self.stdout.write(
            self.style.SUCCESS("DOCU-X LOCAL dummy database seed verified.")
        )
        self.stdout.write(
            json.dumps(
                {
                    "database": "docux",
                    "demo_login": "docux_demo_admin",
                    "credentials_file": CREDENTIAL_FILENAME,
                    "seed": report,
                    "security_checks": verified.get("security_checks", {}),
                },
                sort_keys=True,
                default=str,
            )
        )
        self.stdout.write(
            "Login at http://localhost:4200/login as docux_demo_admin. "
            "Read the password privately from " + CREDENTIAL_FILENAME + ". "
            "Do not share it or commit the file."
        )
