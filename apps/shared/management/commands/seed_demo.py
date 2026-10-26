"""Synthetic DOCU-X local demo tenant/bootstrap.

This starter seeds tenant/users/departments/notifications only. A full Feature 1–10
seed MUST use each feature's current services, not arbitrary model state writes.
"""

import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

DEMO_MARKER = "https://docux-demo.invalid"
TENANTS = (
    ("docux-meridian-demo", "Meridian Technology Group [DEMO]"),
    ("docux-boundary-demo", "Boundary Partner [DEMO]"),
)
DEPARTMENTS = (
    ("finance", "Finance"),
    ("legal", "Legal"),
    ("hr", "Human Resources"),
    ("sales", "Sales & Proposals"),
    ("compliance", "Compliance"),
    ("operations", "Operations"),
    ("management", "Management"),
)
USERS = (
    # username, role, department slugs, membership role, tenant slug
    (
        "docux_demo_admin",
        "company_admin",
        ["management", "finance", "legal", "hr", "sales", "compliance", "operations"],
        "department_admin",
        "docux-meridian-demo",
    ),
    ("docux_demo_finance", "member", ["finance"], "member", "docux-meridian-demo"),
    ("docux_demo_legal", "member", ["legal"], "member", "docux-meridian-demo"),
    ("docux_demo_hr", "member", ["hr"], "member", "docux-meridian-demo"),
    ("docux_demo_sales", "member", ["sales"], "member", "docux-meridian-demo"),
    (
        "docux_demo_compliance",
        "member",
        ["compliance"],
        "member",
        "docux-meridian-demo",
    ),
    ("docux_demo_ops", "member", ["operations"], "member", "docux-meridian-demo"),
    (
        "docux_demo_reviewer",
        "reviewer",
        ["finance", "legal", "compliance"],
        "reviewer",
        "docux-meridian-demo",
    ),
    ("docux_demo_viewer", "viewer", ["management"], "viewer", "docux-meridian-demo"),
    (
        "docux_demo_boundary_admin",
        "company_admin",
        ["management"],
        "department_admin",
        "docux-boundary-demo",
    ),
    (
        "docux_demo_boundary_member",
        "member",
        ["finance"],
        "member",
        "docux-boundary-demo",
    ),
)


def require_demo_environment():
    module = os.environ.get("DJANGO_SETTINGS_MODULE", "")
    if not settings.DEBUG or not module.endswith(".local"):
        raise CommandError(
            "Demo operation refused: DEBUG and local Django settings are required."
        )
    if os.environ.get("DOCUX_DEMO_ENABLED") != "YES":
        raise CommandError(
            "Set DOCUX_DEMO_ENABLED=YES explicitly before demo operations."
        )


class Command(BaseCommand):
    help = "Seed and verify explicitly enabled, isolated synthetic DOCU-X data."

    def add_arguments(self, parser):
        parser.add_argument("--confirm-demo", action="store_true")
        parser.add_argument("--verify", action="store_true")
        parser.add_argument("--reset", action="store_true")
        parser.add_argument("--confirm-reset", action="store_true")
        parser.add_argument("--full", action="store_true")

    def handle(self, *args, **options):
        import json
        from apps.shared.services.demo_data_service import DemoDataService

        if not options["confirm_demo"]:
            raise CommandError("Explicit --confirm-demo is required.")
        require_demo_environment()
        service = DemoDataService()
        if options["reset"]:
            if not options["confirm_reset"]:
                raise CommandError("Reset additionally requires --confirm-reset.")
            result = service.reset(confirmed=True)
        elif options["verify"]:
            result = service.verify(full=options["full"])
        else:
            result = service.seed(full=options["full"])
        self.stdout.write(json.dumps(result, sort_keys=True))
