"""Local synthetic data orchestration. No permission or transition shortcuts."""

import csv
import hashlib
import json
import os
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from django.apps import apps
from django.conf import settings
from django.core.files import File
from django.core.management.base import CommandError
from django.db import connection, models, transaction
from django.db.models import Q
from django.utils import timezone

from apps.shared.models import DemoDataset


class DemoDataService:
    KEY = "docux-synthetic-2026.10"
    ROOT = Path(settings.BASE_DIR) / "demo_fixtures"

    def __init__(self):
        from apps.shared.management.commands.seed_demo import TENANTS, USERS

        self.tenants, self.users = TENANTS, USERS
        self.counts = defaultdict(int)

    @staticmethod
    def guard():
        if (
            not settings.DEBUG
            or os.environ.get("DJANGO_SETTINGS_MODULE") != "config.settings.local"
            or settings.SETTINGS_MODULE != "config.settings.local"
            or os.environ.get("DOCUX_DEMO_ENABLED") != "YES"
        ):
            raise CommandError(
                "Demo operation requires DEBUG, config.settings.local and DOCUX_DEMO_ENABLED=YES."
            )
        if not str(connection.settings_dict["NAME"]).startswith(
            ("docux_demo_", "test_")
        ):
            raise CommandError(
                "Demo operation requires a dedicated docux_demo_* or isolated test_* database."
            )

    @staticmethod
    def stable(key):
        return uuid5(NAMESPACE_URL, "https://docux-demo.invalid/2026.10/" + key)

    def catalog(self):
        from demo_fixtures.validate_fixtures import validate

        validate(self.ROOT)
        return json.loads((self.ROOT / "manifest.json").read_text(encoding="utf-8"))

    def inventory(self, company_ids):
        """Reverse-FK closure includes indirectly owned and M2M child rows.

        Never follow outward references into shared taxonomies or other tenants.
        An unexpected reverse reference is deliberately included so reset refuses it.
        """
        result = {"tenants.company": set(map(str, company_ids))}
        candidates = [
            m
            for m in apps.get_models(include_auto_created=True)
            if m._meta.managed
            and not m._meta.proxy
            and m is not DemoDataset
            and m._meta.label_lower
            not in {
                "token_blacklist.outstandingtoken",
                "token_blacklist.blacklistedtoken",
            }
        ]
        # Short-lived JWT records are owned by their demo user and cascade with
        # that user. Logins must not invalidate the persistent domain ledger.
        candidates = [m for m in candidates if m._meta.app_label != "token_blacklist"]
        for _ in range(len(candidates)):
            changed = False
            for model in candidates:
                query = Q(pk__in=[])
                for field in model._meta.fields:
                    if (
                        field.is_relation
                        and field.many_to_one
                        or field.is_relation
                        and field.one_to_one
                    ):
                        parent = field.remote_field.model._meta.label_lower
                        if result.get(parent):
                            query |= Q(**{field.attname + "__in": result[parent]})
                found = set(
                    map(
                        str,
                        model._base_manager.filter(query).values_list("pk", flat=True),
                    )
                )
                label = model._meta.label_lower
                old = result.setdefault(label, set())
                if found - old:
                    old.update(found)
                    changed = True
            if not changed:
                break
        return {k: sorted(v) for k, v in sorted(result.items()) if v}

    def company_ids(self):
        Company = apps.get_model("tenants", "Company")
        return list(
            Company.objects.filter(slug__in=[s for s, _ in self.tenants]).values_list(
                "pk", flat=True
            )
        )

    def assert_owned(self, ledger):
        Company = apps.get_model("tenants", "Company")
        for company in Company.objects.filter(pk__in=self.company_ids()):
            if (
                company.website != "https://docux-demo.invalid"
                or not company.name.endswith("[DEMO]")
            ):
                raise CommandError(
                    "Tenant no longer carries the expected synthetic markers; refusing mutation/reset."
                )
        if ledger.inventory != self.inventory(self.company_ids()):
            raise CommandError(
                "Demo ownership changed: untracked or missing resources exist. Refusing mutation/reset."
            )

    @transaction.atomic
    def seed(self, full=False):
        self.guard()
        catalog = self.catalog()
        if str(connection.settings_dict["NAME"]).startswith("docux_demo_"):
            Company = apps.get_model("tenants", "Company")
            User = apps.get_model("accounts", "User")
            if (
                Company.objects.exclude(
                    slug__in=[slug for slug, _ in self.tenants]
                ).exists()
                or User.objects.exclude(username__startswith="docux_demo_").exists()
            ):
                raise CommandError(
                    "Dedicated demo database contains untracked accounts/tenants; refusing seeding."
                )
        password = os.environ.get("DOCUX_DEMO_PASSWORD", "")
        if len(password) < 12:
            raise CommandError(
                "DOCUX_DEMO_PASSWORD must contain at least 12 characters; no default exists."
            )
        ledger, created = DemoDataset.objects.select_for_update().get_or_create(
            key=self.KEY
        )
        if created and self.company_ids():
            raise CommandError(
                "Existing demo tenant has no ownership ledger; refusing to adopt unknown records."
            )
        if not created:
            self.assert_owned(ledger)
        previous_count = sum(map(len, ledger.inventory.values()))
        if "core" not in ledger.stages:
            self.core(password)
            ledger.stages.append("core")
        else:
            self.counts["already_exists"] += 1
        if full:
            for name in (
                "documents",
                "workflows",
                "reviewed_inputs",
                "hr",
                "rfp",
                "finance",
                "compliance",
                "proof",
                "scenarios",
            ):
                if name in ledger.stages:
                    self.counts["already_exists"] += 1
                    continue
                getattr(self, name)(ledger, catalog)
                ledger.stages.append(name)
                self.counts["created"] += 1
        ledger.inventory = self.inventory(self.company_ids())
        ledger.save()
        self.counts["created"] = (
            sum(map(len, ledger.inventory.values())) - previous_count
        )
        self.counts["already_exists"] = previous_count
        self.counts["unavailable"] = len(ledger.unavailable)
        return self.report(ledger)

    def core(self, password):
        from apps.shared.management.commands.seed_demo import DEPARTMENTS, DEMO_MARKER

        Company = apps.get_model("tenants", "Company")
        User = apps.get_model("accounts", "User")
        Department = apps.get_model("workspaces", "Department")
        Membership = apps.get_model("workspaces", "DepartmentMembership")
        companies, departments = {}, {}
        for slug, name in self.tenants:
            company = Company.objects.create(
                slug=slug,
                name=name,
                website=DEMO_MARKER,
                legal_name="Synthetic demo only",
                allow_cloud_llm=False,
                allow_local_llm=False,
            )
            companies[slug] = company
            for key, title in DEPARTMENTS:
                if slug == self.tenants[1][0] and key not in {"management", "finance"}:
                    continue
                departments[slug, key] = Department.objects.create(
                    company=company,
                    slug=key,
                    name=title,
                    description="Synthetic demo department",
                    department_type=key if key != "compliance" else "custom",
                )
        for username, role, slugs, membership_role, tenant in self.users + (
            ("docux_demo_revoked", "member", ["finance"], "member", self.tenants[0][0]),
        ):
            if User.objects.filter(username=username).exists():
                raise CommandError(
                    "Demo username collision; refusing to adopt another account."
                )
            if username == "docux_demo_finance":
                role = "department_admin"
                membership_role = "department_admin"
            user = User.objects.create_user(
                username=username,
                email=username + "@example.test",
                password=password,
                company=companies[tenant],
                role=role,
            )
            for slug in slugs:
                Membership.objects.create(
                    company=user.company,
                    department=departments[tenant, slug],
                    user=user,
                    role=membership_role,
                )
        self.counts["created"] += 2 + len(departments) + len(self.users)

    def admin(self):
        return apps.get_model("accounts", "User").objects.get(
            username="docux_demo_admin"
        )

    def department(self, slug):
        return apps.get_model("workspaces", "Department").objects.get(
            company=self.admin().company, slug=slug
        )

    def document(self, ledger, key):
        return apps.get_model("documents", "Document").objects.get(
            pk=ledger.references["documents"][key]
        )

    def documents(self, ledger, catalog):
        from apps.documents.models import SensitivityPolicy
        from apps.document_processing.services.text_extraction_service import (
            TextExtractionService,
        )
        from apps.documents.models import Document, DocumentFieldPolicy, RetentionPolicy
        from apps.vector_search.services.vector_indexing_service import (
            VectorIndexingService,
        )

        User = apps.get_model("accounts", "User")
        admin = self.admin()
        retention = RetentionPolicy.objects.create(
            company=admin.company,
            name="[DEMO] Seven year source retention",
            duration_days=2555,
        )
        for label in ("internal", "confidential", "restricted"):
            SensitivityPolicy.objects.create(
                company=admin.company,
                code=label,
                name="[DEMO] " + label,
                security_class=label,
                minimum_access="admin" if label == "restricted" else "view",
                allow_ai=label != "restricted",
                allow_export=label != "restricted",
                allow_cloud=False,
            )
        saved = {}
        sources = catalog["documents"] + [
            {
                "id": row["id"],
                "path": row["path"],
                "title": "[DEMO] " + row["id"],
                "tenant": self.tenants[0][0],
                "department": "finance",
                "classification": "internal",
            }
            for row in catalog["datasets"]
            if row["id"] != "compliance_framework"
        ]
        sources.append(
            {
                **catalog["documents"][0],
                "id": "boundary_invoice",
                "tenant": self.tenants[1][0],
            }
        )
        for spec in sources:
            owner = (
                admin
                if spec["tenant"] == self.tenants[0][0]
                else User.objects.get(username="docux_demo_boundary_admin")
            )
            department = apps.get_model("workspaces", "Department").objects.get(
                company=owner.company, slug=spec["department"]
            )
            path = self.ROOT / spec["path"]
            kind = (
                "invoice"
                if spec["id"].startswith("invoice")
                else "contract"
                if spec["id"].startswith("contract")
                else "policy"
                if spec["id"].startswith("hr_")
                else "other"
            )
            document = Document(
                company=owner.company,
                department=department,
                uploaded_by=owner,
                owner_user=owner,
                uuid=self.stable("document/" + spec["id"]),
                title=spec["title"],
                description="Synthetic DOCU-X demo source",
                original_filename=path.name,
                document_type=kind,
                sensitivity_label=spec["classification"],
                retention_policy=retention if owner == admin else None,
                retention_duration_days=2555,
                retention_start=timezone.now(),
                status="uploaded",
                checksum=hashlib.sha256(path.read_bytes()).hexdigest(),
                file_size=path.stat().st_size,
            )
            with path.open("rb") as stream:
                document.file.save(
                    "docux-demo/" + str(document.uuid) + "/" + path.name,
                    File(stream),
                    save=False,
                )
            document.save()
            extraction = TextExtractionService.extract_text(document)
            if not extraction.extracted_text.strip():
                raise CommandError(
                    "Synthetic source produced no text; refusing ready state."
                )
            document.status, document.is_ai_ready, document.is_ocr_completed = (
                "ready",
                True,
                True,
            )
            document.processing_stage = "ready"
            document.save()
            VectorIndexingService.index_document(
                document, provider_name="local_hashing"
            )
            saved[spec["id"]] = document.pk
        ledger.references["documents"] = saved
        if "sensitive_hr" in saved:
            DocumentFieldPolicy.objects.create(
                document_id=saved["sensitive_hr"],
                field_key="compensation",
                role="viewer",
                can_view=False,
                can_use_ai=False,
                can_export=False,
            )

    @staticmethod
    def advance(run):
        from apps.workflows.services.workflow_executor_service import (
            WorkflowExecutorService,
        )

        for _ in range(60):
            run.refresh_from_db()
            if run.status != "queued":
                return run
            WorkflowExecutorService.execute(run.company_id, str(run.uuid), run.revision)
        raise CommandError("Synthetic workflow exceeded its bounded execution limit.")

    def workflows(self, ledger, catalog):
        from apps.workflows.services.invoice_definition_service import (
            InvoiceDefinitionService,
        )
        from apps.workflows.services.contract_definition_service import (
            ContractDefinitionService,
        )
        from apps.workflows.services.clause_definition_service import (
            ClauseDefinitionService,
        )
        from apps.workflows.services.clause_playbook_service import (
            ClausePlaybookService,
        )
        from apps.workflows.services.workflow_engine_service import (
            WorkflowEngineService,
        )

        admin = self.admin()
        reviewer = {"user": admin.pk}
        from apps.workflows.models import ClauseType

        clause_type = ClauseType.objects.get(company=None, code="termination")
        book = ClausePlaybookService.create(
            admin,
            name="[DEMO] Legal review policy",
            department=self.department("legal").pk,
            draft={
                "rules": [
                    {
                        "code": "demo_termination",
                        "clause_type": str(clause_type.uuid),
                        "requirement": "required",
                        "severity": "high",
                    }
                ],
                "document_types": ["contract"],
            },
        )
        published = ClausePlaybookService.publish(admin, book, book.revision)
        book.refresh_from_db()
        ClausePlaybookService.activate(admin, book, book.revision, version=published)
        ledger.references["playbook"] = book.pk
        for kind, service, department, source_keys in (
            (
                "invoice",
                InvoiceDefinitionService,
                "finance",
                ["invoice_valid", "invoice_duplicate", "invoice_mismatch"],
            ),
            ("contract", ContractDefinitionService, "legal", ["contract_master"]),
            ("legal", ClauseDefinitionService, "legal", ["contract_master"]),
        ):
            _, version = service.create(
                admin,
                name="[DEMO] " + kind.title(),
                code="docux-demo-" + kind,
                reviewer=reviewer,
                approver=reviewer,
                department=self.department(department).pk,
                policy={
                    "use_ai": False,
                    **(
                        {"playbook_version": str(published.uuid)}
                        if kind == "legal"
                        else {}
                    ),
                },
            )
            ledger.references.setdefault("versions", {})[kind] = version.pk
            for key in source_keys:
                run, _ = WorkflowEngineService.start(
                    admin,
                    version_uuid=version.uuid,
                    document_uuids=[str(self.document(ledger, key).uuid)],
                    idempotency_key="docux-demo-" + kind + "-" + key,
                )
                self.advance(run)

    def reviewed_inputs(self, ledger, catalog):
        import re
        from apps.documents.models import Document
        from apps.document_processing.services.text_extraction_service import (
            TextExtractionService,
        )
        from apps.vector_search.services.vector_indexing_service import (
            VectorIndexingService,
        )
        from apps.workflows.models import (
            InvoiceRecord,
            InvoiceVendor,
            WorkflowVersion,
            ContractReview,
        )
        from apps.workflows.services.workflow_engine_service import (
            WorkflowEngineService,
        )
        from apps.workflows.services.invoice_workflow_service import (
            InvoiceWorkflowService,
        )
        from apps.workflows.services.contract_workflow_service import (
            ContractWorkflowService,
        )
        from apps.workflows.services.contract_extraction_service import evidence
        from apps.workflows.services.source_proof_service import SourceProofService

        admin = self.admin()
        path = self.ROOT / catalog["documents"][0]["path"]
        duplicate = Document(
            company=admin.company,
            department=self.department("legal"),
            uploaded_by=admin,
            owner_user=admin,
            uuid=self.stable("document/reviewed-duplicate"),
            title="[DEMO] Exact duplicate invoice review candidate",
            original_filename=path.name,
            document_type="invoice",
            status="uploaded",
            checksum=hashlib.sha256(path.read_bytes()).hexdigest(),
            file_size=path.stat().st_size,
        )
        with path.open("rb") as stream:
            duplicate.file.save(
                "docux-demo/" + str(duplicate.uuid) + "/" + path.name,
                File(stream),
                save=False,
            )
        duplicate.save()
        TextExtractionService.extract_text(duplicate)
        duplicate.status = "ready"
        duplicate.processing_stage = "ready"
        duplicate.is_ai_ready = True
        duplicate.is_ocr_completed = True
        duplicate.save()
        VectorIndexingService.index_document(duplicate, provider_name="local_hashing")
        ledger.references["documents"]["reviewed_duplicate"] = duplicate.pk
        version = WorkflowVersion.objects.get(
            pk=ledger.references["versions"]["invoice"]
        )
        run, _ = WorkflowEngineService.start(
            admin,
            version_uuid=version.uuid,
            document_uuids=[str(duplicate.uuid)],
            idempotency_key="docux-demo-reviewed-duplicate",
        )
        self.advance(run)
        patterns = {
            "invoice_number": r"Invoice Number:\s*([^|\r\n]+)",
            "invoice_date": r"Invoice Date:\s*(\d{4}-\d{2}-\d{2})",
            "due_date": r"Due Date:\s*(\d{4}-\d{2}-\d{2})",
            "vendor_name": r"Vendor:\s*([^|\r\n]+)",
            "vendor_identifier": r"Vendor ID:\s*([^|\r\n]+)",
            "currency": r"\b(INR)\b",
            "bill_to_name": r"Bill To:\s*([^|\r\n]+)",
            "purchase_order_number": r"Purchase Order:\s*([^|\r\n]+)",
            "subtotal": r"Subtotal:\s*(?:INR\s*)?([0-9]+(?:\.[0-9]+)?)",
            "tax_total": r"(?:GST[^:|\r\n]*|Tax Total):\s*(?:INR\s*)?([0-9]+(?:\.[0-9]+)?)",
            "grand_total": r"Grand Total:\s*(?:INR\s*)?([0-9]+(?:\.[0-9]+)?)",
            "payment_terms": r"Payment Terms:\s*([^|\r\n]+)",
        }
        claims = []
        for invoice in InvoiceRecord.objects.filter(
            company=admin.company,
            workflow_run__status__in=["review_required", "exception"],
            extraction_run__isnull=False,
        ):
            text = invoice.document.text_extraction.extracted_text
            for key, pattern in patterns.items():
                match = re.search(pattern, text, re.I)
                if not match:
                    continue
                field = invoice.extraction_run.result_fields.filter(
                    field_key=key
                ).first()
                if not field:
                    continue
                value = match[1].strip()
                if key == "vendor_identifier":
                    vendor_name = re.search(patterns["vendor_name"], text, re.I)
                    if vendor_name:
                        InvoiceVendor.objects.get_or_create(
                            company=admin.company,
                            identifier=value,
                            defaults={"name": vendor_name[1].strip()},
                        )
                invoice.workflow_run.refresh_from_db()
                InvoiceWorkflowService.correct(
                    admin,
                    field,
                    value,
                    invoice.workflow_run.revision,
                    "Synthetic source-backed review of pipe-separated PDF labels; exact source offsets "
                    + str(match.start(1))
                    + ":"
                    + str(match.end(1))
                    + ".",
                )
                if key == "grand_total":
                    row = SourceProofService.capture(
                        admin,
                        kind="document",
                        source_uuid=invoice.document.uuid,
                        locator_type="text_offsets",
                        locator={
                            "document_uuid": str(invoice.document.uuid),
                            "start": match.start(1),
                            "end": match.end(1),
                        },
                        precision="exact",
                        excerpt=match[1],
                        method="user_correction",
                    )
                    claim = SourceProofService.claim(admin, row, "invoice_total", value)
                    claims.append(claim.pk)
        ledger.references["reviewed_invoice_claims"] = claims
        patterns = {
            "contract_title": r"(?m)^(Master Services Agreement[^\r\n]+)",
            "effective_date": r"Effective Date:\s*(\d{4}-\d{2}-\d{2})",
            "termination_notice_period": r"at least (\d+ days) before renewal",
        }
        for review in ContractReview.objects.filter(
            company=admin.company,
            workflow_run__status__in=["review_required", "exception"],
            extraction_run__isnull=False,
        ):
            text = review.document.text_extraction.extracted_text
            for key, pattern in patterns.items():
                match = re.search(pattern, text, re.I)
                field = review.extraction_run.result_fields.filter(
                    field_key=key
                ).first()
                if not match or not field:
                    continue
                review.workflow_run.refresh_from_db()
                ContractWorkflowService.correct(
                    admin,
                    field,
                    match[1],
                    review.workflow_run.revision,
                    "Synthetic review of the exact supplied contract source span.",
                    proofs=[
                        evidence(review.document, text, match.start(1), match.end(1))
                    ],
                )
        if ledger.references.get("finance"):
            from apps.workflows.models import FinanceAudit
            from apps.workflows.services.finance_audit_service import (
                FinanceAuditService,
            )

            audit = FinanceAudit.objects.get(pk=ledger.references["finance"])
            FinanceAuditService.execute(
                admin,
                audit,
                revision=audit.revision,
                idempotency_key="docux-demo-reviewed-input-tests",
            )

    def hr(self, ledger, catalog):
        from apps.workflows.models import HRPolicyCategory, HRPolicyVersion
        from apps.workflows.services.hr_policy_service import HRPolicyService
        from apps.workflows.services.hr_policy_workflow_service import (
            HRPolicyWorkflowService,
        )
        from apps.workflows.services.workflow_engine_service import (
            WorkflowEngineService,
        )

        from apps.workflows.models import HRPolicySettings

        admin = self.admin()
        HRPolicySettings.objects.get_or_create(company=admin.company)
        category = HRPolicyCategory.objects.get(
            company=None, kind="policy", code="remote_work"
        )
        policy = HRPolicyService.create(
            admin,
            title="[DEMO] Remote work policy",
            code="docux-demo-remote-work",
            category_uuid=category.uuid,
            department=self.department("hr").pk,
            draft={
                "document_uuid": str(self.document(ledger, "hr_policy_2025").uuid),
                "effective_from": "2025-01-01",
            },
        )
        versions = []
        for key, date in (
            ("hr_policy_2025", "2025-01-01"),
            ("hr_policy_2026", "2026-01-01"),
        ):
            if versions:
                policy.refresh_from_db()
                HRPolicyService.edit(
                    admin,
                    policy,
                    policy.revision,
                    {
                        "document_uuid": str(self.document(ledger, key).uuid),
                        "effective_from": date,
                        "supersedes": str(versions[-1].uuid),
                    },
                )
            policy.refresh_from_db()
            run = HRPolicyWorkflowService.submit(
                admin,
                policy,
                revision=policy.revision,
                reviewer={"user": admin.pk},
                approver={"user": admin.pk},
                idempotency_key="docux-demo-" + key,
            )
            self.advance(run)
            # These are explicitly synthetic reviewer actions through the same
            # validation/approval engine as the UI, never approved-state writes.
            for action, required in (
                ("accept", "review_required"),
                ("approve", "approval_required"),
            ):
                run.refresh_from_db()
                if run.status != required:
                    raise CommandError(
                        "Synthetic HR publication did not reach its required review boundary."
                    )
                WorkflowEngineService.command(
                    admin,
                    run,
                    action=action,
                    expected_revision=run.revision,
                    idempotency_key="docux-demo-" + key + "-" + action,
                    comment="Synthetic local demo review through validated source publication path.",
                )
                self.advance(run)
            versions.append(HRPolicyVersion.objects.get(workflow_run=run))
        ledger.references["hr_versions"] = [v.pk for v in versions]

    def rfp(self, ledger, catalog):
        from apps.workflows.services.rfp_service import RFPService
        from apps.workflows.services.rfp_workflow_service import RFPWorkflowService

        admin = self.admin()
        main = next(
            k
            for k in ledger.references["documents"]
            if k.startswith("rfp_") and k != "rfp_addendum"
        )
        sources = [
            {"document_uuid": str(self.document(ledger, main).uuid), "role": "rfp_main"}
        ]
        if "rfp_addendum" in ledger.references["documents"]:
            sources.append(
                {
                    "document_uuid": str(self.document(ledger, "rfp_addendum").uuid),
                    "role": "rfp_addendum",
                }
            )
        project = RFPService.create(
            admin,
            title="[DEMO] Procurement proposal",
            sources=sources,
            department=self.department("sales").pk,
            knowledge_document_uuids=[],
        )
        run = RFPWorkflowService.submit(
            admin,
            project,
            revision=project.revision,
            reviewer={"user": admin.pk},
            approver={"user": admin.pk},
            idempotency_key="docux-demo-rfp",
        )
        self.advance(run)
        from apps.workflows.services.contract_extraction_service import evidence
        import re

        project.refresh_from_db()
        source = project.sources.get(role="rfp_main")
        text = source.document.text_extraction.extracted_text
        if not project.metadata:
            RFPService.correct_extraction(
                admin,
                project,
                revision=project.revision,
                source_uuid=source.uuid,
                kind="classification",
                key="principal",
                value={"classified": True},
                evidence=evidence(source.document, text, 0, min(120, len(text))),
                reason="Synthetic reviewer confirms this supplied RFP source package; document content remains untrusted data.",
            )
            project.refresh_from_db()
        if not project.items.exists():
            for index, match in enumerate(
                re.finditer(
                    r"(?im)^.*\b(?:must|shall|required|describe|provide)\b.*$", text
                )
            ):
                project.refresh_from_db()
                RFPService.correct_extraction(
                    admin,
                    project,
                    revision=project.revision,
                    source_uuid=source.uuid,
                    kind="requirement",
                    key="demo-" + str(index),
                    value={
                        "section": "Reviewed source requirements",
                        "kind": "requirement",
                        "mandatory": bool(
                            re.search(r"\b(?:must|shall|required)\b", match[0], re.I)
                        ),
                        "response_required": True,
                        "category": "general",
                    },
                    evidence=evidence(
                        source.document, text, match.start(), match.end()
                    ),
                    reason="Synthetic review of an exact source line; no generated company response.",
                )
                if index >= 19:
                    break
        ledger.references["rfp"] = project.pk
        ledger.unavailable["optional_ai_proposal_answers"] = (
            "No model calls or unsupported company capability answers are generated by default."
        )

    def finance(self, ledger, catalog):
        from apps.workflows.services.finance_schema import FIELDS
        from apps.workflows.services.finance_audit_service import (
            FinanceAuditService as service,
        )
        from apps.workflows.services.finance_workflow_service import (
            FinanceWorkflowService,
        )

        admin = apps.get_model("accounts", "User").objects.get(
            username="docux_demo_finance"
        )
        audit = service.create(
            admin,
            title="[DEMO] October finance audit",
            department=self.department("finance").pk,
            scope={"start": "2026-10-01", "end": "2026-10-31", "period": "monthly"},
            policy={"materiality": {"INR": "25000.00"}, "tolerance": {"INR": "0.00"}},
        )
        service.ingest(
            admin,
            audit,
            revision=audit.revision,
            document_uuid=self.document(ledger, "finance_transactions_120").uuid,
            kind="csv",
            mapping={
                key: key
                for key in next(
                    csv.reader(
                        (self.ROOT / "datasets/finance_transactions_120.csv").open(
                            encoding="utf-8"
                        )
                    )
                )
                if key in FIELDS
            },
            idempotency_key="docux-demo-finance-import",
        )
        audit.refresh_from_db()
        service.sample(
            admin,
            audit,
            revision=audit.revision,
            parameters={"method": "random", "size": 20, "seed": "docux-demo-2026"},
            idempotency_key="docux-demo-finance-sample",
        )
        audit.refresh_from_db()
        run = FinanceWorkflowService.submit(
            admin,
            audit,
            revision=audit.revision,
            reviewer={"user": admin.pk},
            approver={"user": admin.pk},
            idempotency_key="docux-demo-finance",
        )
        self.advance(run)
        audit.refresh_from_db()
        finding = run.findings.first()
        if finding:
            service.request_evidence(
                admin,
                audit,
                finding.uuid,
                revision=audit.revision,
                requested_item="Synthetic audit review: provide the original supporting payment evidence.",
                owner={"user": admin.pk},
                due_date="2026-12-31",
            )
        ledger.references["finance"] = audit.pk

    def compliance(self, ledger, catalog):
        from apps.workflows.services.compliance_library_service import (
            ComplianceLibraryService as library,
        )
        from apps.workflows.services.compliance_assessment_service import (
            ComplianceAssessmentService as service,
        )
        from apps.workflows.services.compliance_workflow_service import (
            ComplianceWorkflowService,
        )

        admin = self.admin()
        fixture = json.loads(
            (self.ROOT / "datasets/compliance_framework_synthetic.json").read_text(
                encoding="utf-8"
            )
        )
        framework = library.create_framework(
            admin,
            code=fixture["framework_code"],
            name=fixture["framework_name"],
            draft={
                "requirements": [
                    {"code": r["id"], "title": r["title"]}
                    for r in fixture["requirements"]
                ]
            },
        )
        version = library.publish(admin, framework, revision=framework.revision)
        for requirement in version.requirements.all():
            control = library.create_control(
                admin,
                code="DEMO-" + requirement.code,
                name="[DEMO] " + requirement.title,
                department=self.department("compliance").pk,
                definition={
                    "objective": requirement.title,
                    "owner": {"user": admin.pk},
                    "tests": [
                        "missing_evidence",
                        "expired_evidence",
                        "overdue_review",
                        "unmapped_control",
                    ],
                },
            )
            library.map_control(
                admin,
                control,
                revision=control.revision,
                requirement_uuid=requirement.uuid,
            )
        assessment = service.create(
            admin,
            title="[DEMO] Document controls assessment",
            framework_version_uuid=version.uuid,
            scope={
                "start": "2026-10-01",
                "end": "2026-10-31",
                "document_uuids": [
                    str(self.document(ledger, k).uuid)
                    for k in ("current_evidence", "expired_evidence")
                ],
            },
            idempotency_key="docux-demo-assessment",
            department=self.department("compliance").pk,
        )
        requirement = version.requirements.get(code="AUTH-01")
        control = apps.get_model("workflows", "ComplianceControl").objects.get(
            company=admin.company, code="DEMO-AUTH-01"
        )
        for key in ("current_evidence", "expired_evidence"):
            assessment.refresh_from_db()
            service.link(
                admin,
                assessment,
                revision=assessment.revision,
                requirement_uuid=requirement.uuid,
                control_uuid=control.uuid,
                source_kind="document",
                source_uuid=self.document(ledger, key).uuid,
                idempotency_key="docux-demo-link-" + key,
                dates={"expires": "2024-12-31"}
                if key == "expired_evidence"
                else {"expires": "2027-12-31"},
            )
        assessment.refresh_from_db()
        run = ComplianceWorkflowService.submit(
            admin,
            assessment,
            revision=assessment.revision,
            reviewer={"user": admin.pk},
            approver={"user": admin.pk},
            idempotency_key="docux-demo-compliance",
        )
        self.advance(run)
        assessment.refresh_from_db()
        evaluation = assessment.evaluations.order_by("-pk").first()
        if evaluation and evaluation.data.get("rows"):
            target = evaluation.data["rows"][0]["target"]
            for kind in ("evidence_request", "remediation"):
                assessment.refresh_from_db()
                service.decide(
                    admin,
                    assessment,
                    revision=assessment.revision,
                    kind=kind,
                    target=target,
                    outcome="open",
                    reason="Synthetic evidence gap requires reviewer follow-up; no satisfied or certification claim.",
                    idempotency_key="docux-demo-" + kind,
                    data={"owner": {"user": admin.pk}, "due": "2026-12-31"},
                )
        ledger.references["compliance"] = assessment.pk

    def proof(self, ledger, catalog):
        from apps.workflows.services.source_proof_service import (
            SourceProofService as service,
        )

        admin = self.admin()
        references = {}
        for key, expiry in (
            ("invoice_valid", None),
            ("expired_evidence", timezone.now() - timedelta(days=365)),
        ):
            doc = self.document(ledger, key)
            text = doc.text_extraction.extracted_text
            excerpt = text[: min(120, len(text))]
            row = service.capture(
                admin,
                kind="document",
                source_uuid=doc.uuid,
                locator_type="text_offsets",
                locator={
                    "document_uuid": str(doc.uuid),
                    "start": 0,
                    "end": len(excerpt),
                },
                precision="exact",
                excerpt=excerpt,
                method="deterministic_parser",
                valid_until=expiry,
            )
            claim = service.claim(admin, row, "synthetic_source_excerpt", excerpt)
            references[key] = {"evidence": row.pk, "claim": claim.pk}
        from apps.documents.models import DocumentAccessPolicy
        from apps.documents.services.document_governance_service import (
            DocumentGovernanceService,
        )
        from apps.accounts.models import User
        from apps.ai_engine.models import AIConversation, AIMessage

        doc = self.document(ledger, "prompt_injection")
        text = doc.text_extraction.extracted_text
        row = service.capture(
            admin,
            kind="document",
            source_uuid=doc.uuid,
            locator_type="text_offsets",
            locator={
                "document_uuid": str(doc.uuid),
                "start": 0,
                "end": min(100, len(text)),
            },
            precision="exact",
            excerpt=text[:100],
            method="deterministic_parser",
        )
        references["stale"] = {"evidence": row.pk}
        DocumentGovernanceService.update(
            admin, doc, {"custodian": admin}, expected_revision=doc.governance_revision
        )
        sales = User.objects.get(username="docux_demo_sales")
        revoked_doc = self.document(ledger, "invoice_duplicate")
        grant = DocumentAccessPolicy.objects.create(
            document=revoked_doc,
            user=sales,
            access_level="view",
            can_download=False,
            can_export=False,
            can_use_ai=False,
        )
        text = revoked_doc.text_extraction.extracted_text
        row = service.capture(
            sales,
            kind="document",
            source_uuid=revoked_doc.uuid,
            locator_type="text_offsets",
            locator={
                "document_uuid": str(revoked_doc.uuid),
                "start": 0,
                "end": min(100, len(text)),
            },
            precision="exact",
            excerpt=text[:100],
            method="deterministic_parser",
        )
        references["revoked_access"] = {
            "evidence": row.pk,
            "user": sales.pk,
            "grant": grant.pk,
        }
        grant.soft_delete()
        questions = json.loads(
            (self.ROOT / "scenarios/demo_questions.json").read_text(encoding="utf-8")
        )
        for question in [q for q in questions if q["module"] == "internal_ai"]:
            conversation = AIConversation.objects.create(
                company=admin.company,
                user=admin,
                scope="internal",
                title="[DEMO] " + question["question"][:180],
                metadata={
                    "synthetic_demo": True,
                    "document_scope": "selected",
                    "document_uuids": [
                        str(self.document(ledger, "hr_policy_2026").uuid)
                    ],
                },
            )
            AIMessage.objects.create(
                conversation=conversation,
                role="user",
                content=question["question"],
                metadata={
                    "requested_document_uuids": [
                        str(self.document(ledger, "hr_policy_2026").uuid)
                    ]
                },
            )
        for item in references.values():
            service.validate(
                admin,
                apps.get_model("workflows", "EvidenceRecord").objects.get(
                    pk=item["evidence"]
                ),
            )
        ledger.references["proof"] = references
        ledger.unavailable["optional_internal_ai_answers"] = (
            "No synthetic model answers are persisted without a real authorized retrieval/generation path."
        )

    def scenarios(self, ledger, catalog):
        from apps.accounts.models import User
        from apps.workflows.models import WorkflowVersion
        from apps.workspaces.models import DepartmentMembership
        from apps.workflows.services.workflow_engine_service import (
            WorkflowEngineService as engine,
        )
        from apps.workflows.services.workflow_definition_service import (
            WorkflowDefinitionService as definitions,
        )
        from apps.documents.models import Document
        from apps.documents.services.document_processing_job_service import (
            DocumentProcessingJobService,
        )

        admin = self.admin()
        version = WorkflowVersion.objects.get(
            pk=ledger.references["versions"]["invoice"]
        )
        source = self.document(ledger, "invoice_valid")
        revoked = User.objects.get(username="docux_demo_revoked")
        from apps.documents.models import DocumentAccessPolicy

        grant = DocumentAccessPolicy.objects.create(
            document=source,
            user=revoked,
            access_level="admin",
            can_use_ai=True,
            can_export=False,
        )
        failed, _ = engine.start(
            revoked,
            version_uuid=version.uuid,
            document_uuids=[str(source.uuid)],
            idempotency_key="docux-demo-revoked-run",
        )
        DepartmentMembership.objects.get(
            user=revoked, department=self.department("finance")
        ).soft_delete()
        grant.soft_delete()
        self.advance(failed)
        failed.refresh_from_db()
        if failed.status != "failed":
            raise CommandError("Revoked workflow did not fail closed.")
        queued, _ = engine.start(
            admin,
            version_uuid=version.uuid,
            document_uuids=[str(source.uuid)],
            idempotency_key="docux-demo-queued-run",
        )
        definition = definitions.create(
            admin,
            name="[DEMO] General document approval",
            code="docux-demo-approval",
            workflow_type="generic",
            department_id=self.department("finance").pk,
        )
        version = definitions.publish(
            admin,
            definition,
            entry_step="review",
            steps=[
                {
                    "key": "review",
                    "name": "Review document",
                    "step_type": "human_review",
                    "configuration": {"assignment": {"user": admin.pk}},
                },
                {
                    "key": "approve",
                    "name": "Approve document",
                    "step_type": "approval",
                    "configuration": {"assignment": {"user": admin.pk}},
                },
                {"key": "done", "name": "Completed", "step_type": "terminal"},
                {
                    "key": "rejected",
                    "name": "Rejected",
                    "step_type": "terminal",
                    "configuration": {"status": "rejected"},
                },
            ],
            transitions=[
                {"source": "review", "target": "approve", "outcome": "accepted"},
                {"source": "approve", "target": "done", "outcome": "approved"},
                {"source": "review", "target": "rejected", "outcome": "rejected"},
                {"source": "approve", "target": "rejected", "outcome": "rejected"},
            ],
        )
        approval, _ = engine.start(
            admin,
            version_uuid=version.uuid,
            document_uuids=[str(source.uuid)],
            idempotency_key="docux-demo-pending-approval",
        )
        self.advance(approval)
        approval.refresh_from_db()
        engine.command(
            admin,
            approval,
            action="accept",
            expected_revision=approval.revision,
            idempotency_key="docux-demo-pending-approval-review",
            comment="Synthetic source review; final approval intentionally remains pending.",
        )
        self.advance(approval)
        path = self.ROOT / catalog["documents"][0]["path"]
        pending = Document(
            company=admin.company,
            department=self.department("finance"),
            uploaded_by=admin,
            owner_user=admin,
            uuid=self.stable("document/pending-processing"),
            title="[DEMO] Awaiting document processing",
            original_filename=path.name,
            status="uploaded",
            checksum=hashlib.sha256(path.read_bytes()).hexdigest(),
            file_size=path.stat().st_size,
        )
        with path.open("rb") as stream:
            pending.file.save(
                "docux-demo/" + str(pending.uuid) + "/" + path.name,
                File(stream),
                save=False,
            )
        pending.save()
        DocumentProcessingJobService.create_default_jobs_for_document(pending)
        ledger.references["scenario_runs"] = {
            "failed": failed.pk,
            "queued": queued.pk,
            "approval": approval.pk,
        }
        ledger.references["pending_document"] = pending.pk

    def report(self, ledger):
        return {
            "counts": {key: len(value) for key, value in ledger.inventory.items()},
            "operations": {
                key: self.counts[key]
                for key in (
                    "created",
                    "already_exists",
                    "skipped",
                    "failed",
                    "unavailable",
                )
            },
            "unavailable": ledger.unavailable,
            "stages": ledger.stages,
        }

    @transaction.atomic
    def verify(self, full=True):
        self.guard()
        self.catalog()
        ledger = DemoDataset.objects.select_for_update().filter(key=self.KEY).first()
        if not ledger:
            raise CommandError("Demo ownership ledger is missing.")
        self.assert_owned(ledger)
        required = (
            {
                "core",
                "documents",
                "workflows",
                "reviewed_inputs",
                "hr",
                "rfp",
                "finance",
                "compliance",
                "proof",
                "scenarios",
            }
            if full
            else {"core"}
        )
        if required - set(ledger.stages):
            raise CommandError(
                "Required demo stages are missing: "
                + ", ".join(sorted(required - set(ledger.stages)))
            )
        if full and any(not key.startswith("optional_") for key in ledger.unavailable):
            raise CommandError(
                "Required full demo coverage remains unavailable: "
                + ", ".join(sorted(ledger.unavailable))
            )
        result = self.report(ledger)
        if full:
            result["security_checks"] = self.verify_security(ledger)
        return result

    def verify_security(self, ledger):
        from rest_framework.exceptions import PermissionDenied
        from apps.accounts.models import User
        from apps.documents.services import DocumentPermissionService
        from apps.workflows.models import (
            EvidenceRecord,
            ProofClaim,
            HRPolicyVersion,
            WorkflowRun,
        )
        from apps.workflows.services.source_proof_service import (
            SourceProofService as proof,
        )
        from apps.workflows.services.proof_source_service import ProofSourceService
        from apps.shared.services.dashboard_analytics_service import (
            DashboardAnalyticsService,
        )

        admin = self.admin()
        foreign = User.objects.get(username="docux_demo_boundary_admin")
        sales = User.objects.get(username="docux_demo_sales")
        checks = {}
        for key, pk in ledger.references["documents"].items():
            doc = self.document(ledger, key)
            with doc.file.open("rb") as stream:
                if hashlib.sha256(stream.read()).hexdigest() != doc.checksum:
                    raise CommandError("Demo source file digest changed.")
            checks["company_isolation"] = not DocumentPermissionService.can_action(
                foreign if doc.company_id == admin.company_id else admin, doc, "view"
            )
            if not checks["company_isolation"]:
                raise CommandError("Tenant isolation verification failed.")
        checks["department_isolation"] = not DocumentPermissionService.can_action(
            sales, self.document(ledger, "sensitive_hr"), "view"
        )
        references = ledger.references["proof"]
        resolver = ProofSourceService(admin)
        for key, expected in (
            ("invoice_valid", "valid"),
            ("expired_evidence", "expired"),
            ("stale", "stale"),
        ):
            row = EvidenceRecord.objects.get(pk=references[key]["evidence"])
            checks["proof_" + expected] = proof.validity(resolver, row)[0] == expected
        row = EvidenceRecord.objects.get(pk=references["revoked_access"]["evidence"])
        try:
            proof.evidence_data(ProofSourceService(sales), row)
            checks["access_revocation"] = False
        except PermissionDenied:
            checks["access_revocation"] = True
        claim = ProofClaim.objects.get(pk=references["invoice_valid"]["claim"])
        try:
            proof.export(admin, claim, external=True)
            checks["export_restriction"] = False
        except PermissionDenied:
            checks["export_restriction"] = True
        versions = list(
            HRPolicyVersion.objects.filter(
                pk__in=ledger.references["hr_versions"]
            ).order_by("number")
        )
        checks["current_historical"] = (
            len(versions) == 2 and versions[0].number == 1 and versions[1].number == 2
        )
        states = set(
            WorkflowRun.objects.filter(company=admin.company).values_list(
                "status", flat=True
            )
        )
        checks["review_states"] = {
            "review_required",
            "completed",
            "queued",
            "failed",
            "approval_required",
        } <= states
        overview = DashboardAnalyticsService.get_overview(admin)
        checks["dashboard"] = (
            overview["documents"]["total_documents"] > 0
            and overview["extractions"]["total_extraction_runs"] > 0
        )
        required_counts = {
            "workflows.invoicerecord": 3,
            "workflows.contractreview": 1,
            "workflows.clauseanalysis": 1,
            "workflows.hrpolicyversion": 2,
            "workflows.rfpproject": 1,
            "workflows.rfpitem": 1,
            "workflows.financetransaction": 120,
            "workflows.financesample": 1,
            "workflows.financetestrun": 1,
            "workflows.complianceassessment": 1,
            "workflows.complianceevidence": 2,
            "workflows.compliancedecision": 2,
            "notifications.notification": 1,
            "approvals.approvalrequest": 1,
        }
        for label, minimum in required_counts.items():
            checks["coverage_" + label] = (
                len(ledger.inventory.get(label, [])) >= minimum
            )
        if not all(checks.values()):
            raise CommandError(
                "Demo verification failed: "
                + ", ".join(k for k, v in checks.items() if not v)
            )
        return checks

    @transaction.atomic
    def reset(self, confirmed=False):
        self.guard()
        if not confirmed:
            raise CommandError("Reset requires an additional explicit confirmation.")
        ledger = DemoDataset.objects.select_for_update().filter(key=self.KEY).first()
        if not ledger:
            raise CommandError("No ownership ledger; refusing reset.")
        self.assert_owned(ledger)
        # Lock the complete, previously recorded reverse-FK closure before any
        # cleanup. Unknown children/tokens/uploads cause refusal, never adoption.
        for label, pks in ledger.inventory.items():
            model = apps.get_model(label)
            list(
                model._base_manager.select_for_update()
                .filter(pk__in=pks)
                .values_list("pk", flat=True)
            )
        self.assert_owned(ledger)
        files = list(
            apps.get_model("documents", "Document")
            .objects.filter(company_id__in=self.company_ids())
            .values_list("file", flat=True)
        )
        # Dataset destruction is local-only and separately confirmed. Production
        # immutable managers/services remain unchanged. Break owned nullable
        # back-pointers so protected version/run cycles can be deleted as a unit.
        for label, pks in ledger.inventory.items():
            model = apps.get_model(label)
            for field in model._meta.fields:
                if field.is_relation and field.null and field.remote_field:
                    parent = field.remote_field.model._meta.label_lower
                    if parent in ledger.inventory:
                        models.QuerySet(model=model).filter(
                            pk__in=pks,
                            **{field.attname + "__in": ledger.inventory[parent]},
                        ).update(**{field.attname: None})
        from django.db.models.deletion import ProtectedError, RestrictedError

        pending = dict(ledger.inventory)
        while pending:
            progress = False
            for label, pks in list(pending.items()):
                model = apps.get_model(label)
                try:
                    with transaction.atomic():
                        models.QuerySet(model=model).filter(pk__in=pks).delete()
                except (ProtectedError, RestrictedError):
                    continue
                del pending[label]
                progress = True
            if not progress:
                raise CommandError(
                    "Protected demo dependency could not be safely cleaned; all changes rolled back."
                )
        ledger.delete()
        # File deletion is intentionally not recursive, and cannot target a
        # non-demo file or leave the configured media directory.
        from django.core.files.storage import default_storage

        def cleanup_files():
            for name in files:
                path = Path(default_storage.path(name)).resolve()
                if (
                    path.is_relative_to(Path(settings.MEDIA_ROOT).resolve())
                    and "docux-demo" in path.parts
                ):
                    default_storage.delete(name)

        transaction.on_commit(cleanup_files)
        return {"reset": "owned synthetic dataset", "non_demo_data": "preserved"}
