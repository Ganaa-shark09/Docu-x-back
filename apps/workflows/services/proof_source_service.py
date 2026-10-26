"""Closed typed source registry. No ContentType or client supplied authorization."""

import hashlib
from types import SimpleNamespace
from uuid import UUID

from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.documents.models import Document
from apps.workflows import models
from apps.workflows.services.finance_audit_service import FinanceAuditService, digest
from apps.workflows.services.compliance_evidence_service import (
    ComplianceEvidenceService,
)
from apps.workflows.permissions.workflow_permissions import WorkflowPermissionService


OUTPUTS = {
    "invoice_output": (models.InvoiceOutputVersion, "invoice", "workflow_run"),
    "contract_output": (models.ContractReviewOutput, "contract_review", "workflow_run"),
    "clause_output": (models.ClauseAnalysisOutput, "analysis", "workflow_run"),
    "rfp_output": (models.RFPProposalOutput, "project", "run"),
    "finance_output": (models.FinanceAuditOutput, "audit", "run"),
    "compliance_output": (models.ComplianceOutput, "assessment", "run"),
}
KINDS = tuple(OUTPUTS) + (
    "document",
    "hr_policy_version",
    "workflow_artifact",
    "finance_dataset",
    "finance_transaction",
    "rfp_response",
)


def reference(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError("Invalid source reference.")


def unavailable():
    raise PermissionDenied("Evidence source is unavailable.")


class ProofSourceService:
    """Instances live for one operation and one user/action only."""

    def __init__(self, user, action="view"):
        user.refresh_from_db()
        if not WorkflowPermissionService.active(user):
            unavailable()
        self.user, self.action = user, action
        self.cache, self.doc_cache, self.audit_cache = {}, {}, set()

    def document(self, doc):
        if doc.pk not in self.doc_cache:
            FinanceAuditService.document(self.user, doc, self.action)
            extraction = getattr(doc, "text_extraction", None)
            text = extraction.extracted_text if extraction else ""
            if len(text.encode()) > 2097152:
                raise ValidationError("Source text exceeds proof bounds.")
            # Stream large files; do not import Finance's 8 MiB dataset limit.
            sha = hashlib.sha256()
            if not doc.file and not extraction:
                unavailable()
            try:
                if doc.file:
                    with doc.file.open("rb") as stream:
                        while block := stream.read(65536):
                            sha.update(block)
            except (OSError, ValueError):
                unavailable()
            pin = {
                "document_uuid": str(doc.uuid),
                "governance_revision": doc.governance_revision,
                "checksum": doc.checksum,
                "file_digest": sha.hexdigest() if doc.file else None,
                "text_digest": digest(text),
                "extraction_uuid": str(extraction.uuid) if extraction else None,
                "page_count": extraction.page_count if extraction else 0,
            }
            self.doc_cache[doc.pk] = pin, text
        return self.doc_cache[doc.pk]

    def resolve(self, kind, source_uuid):
        if kind not in KINDS:
            raise ValidationError("Unsupported proof source type.")
        source_uuid = reference(source_uuid)
        key = kind, source_uuid
        if key in self.cache:
            return self.cache[key]
        user = self.user
        current = True
        if kind == "document":
            obj = (
                Document.objects.filter(
                    uuid=source_uuid, company_id=user.company_id, is_deleted=False
                )
                .select_related("text_extraction")
                .first()
            )
            if not obj:
                unavailable()
            docs, data = [obj], None
            version = {}
        elif kind in {"finance_dataset", "finance_transaction"}:
            model = (
                models.FinanceDataset
                if kind == "finance_dataset"
                else models.FinanceTransaction
            )
            path = "audit" if kind == "finance_dataset" else "dataset__audit"
            obj = (
                model.objects.filter(
                    uuid=source_uuid,
                    **{path + "__company_id": user.company_id},
                    is_deleted=False,
                )
                .select_related(path)
                .first()
            )
            if not obj:
                unavailable()
            dataset = obj if kind == "finance_dataset" else obj.dataset
            if dataset.audit_id not in self.audit_cache:
                FinanceAuditService.authorize(user, dataset.audit, self.action)
                self.audit_cache.add(dataset.audit_id)
            docs = [dataset.document]
            version = {
                "dataset_uuid": str(dataset.uuid),
                "dataset_version": dataset.number,
                "checksum": dataset.checksum,
            }
            data = (
                {
                    "mapping": dataset.mapping,
                    "totals": dataset.totals,
                    "row_count": dataset.row_count,
                }
                if kind == "finance_dataset"
                else {
                    "row": obj.row,
                    "transaction_uuid": str(obj.uuid),
                    "data": obj.data,
                    "provenance": obj.provenance,
                    "amount": str(obj.amount) if obj.amount is not None else None,
                }
            )
        elif kind == "rfp_response":
            obj = (
                models.RFPResponseVersion.objects.filter(
                    uuid=source_uuid,
                    item__project__company_id=user.company_id,
                    is_deleted=False,
                )
                .select_related("item__project")
                .first()
            )
            if not obj:
                unavailable()
            # A draft is never promoted into evidence. Resolve only a response
            # present in an actual approved completed proposal output.
            output = next(
                (
                    row
                    for row in obj.item.project.outputs.all()
                    if any(
                        r.get("response_uuid") == str(obj.uuid)
                        for r in row.data.get("responses", [])
                    )
                ),
                None,
            )
            if not output:
                unavailable()
            parent = self.resolve("rfp_output", output.uuid)
            docs = parent["documents"]
            data = {
                "text": obj.text,
                "evidence": obj.evidence,
                "grounding": obj.grounding,
                "externally_shareable": obj.externally_shareable,
            }
            version = {
                "number": obj.number,
                "output_uuid": str(output.uuid),
                "output_identity": digest(parent["pin"]),
            }
        elif kind == "hr_policy_version":
            from apps.workflows.services.hr_policy_service import (
                HRPolicyService,
                effective_status,
            )

            obj = models.HRPolicyVersion.objects.filter(
                uuid=source_uuid, policy__company_id=user.company_id, is_deleted=False
            ).first()
            if not obj:
                unavailable()
            obj = HRPolicyService.authorize(user, obj, self.action)
            if (
                obj.workflow_run.status != "completed"
                or not obj.workflow_run.step_runs.filter(
                    approval__status="approved"
                ).exists()
            ):
                unavailable()
            docs, data = (
                [obj.document],
                {
                    "number": obj.number,
                    "source_digest": obj.source_digest,
                    "effective_from": obj.effective_from.isoformat(),
                    "effective_until": obj.effective_until.isoformat()
                    if obj.effective_until
                    else None,
                    "sections": [
                        {
                            "uuid": str(s.uuid),
                            "heading": s.heading,
                            "evidence": s.evidence,
                            "digest": s.digest,
                        }
                        for s in obj.sections.all()[:100]
                    ],
                },
            )
            version = {"number": obj.number, "policy_uuid": str(obj.policy.uuid)}
            current = effective_status(obj) == "effective"
        else:
            if kind == "workflow_artifact":
                obj = (
                    models.WorkflowArtifact.objects.filter(
                        uuid=source_uuid,
                        run__company_id=user.company_id,
                        is_deleted=False,
                    )
                    .select_related("run__version__definition")
                    .first()
                )
                if not obj:
                    unavailable()
                run = obj.run
            else:
                model, relation, run_field = OUTPUTS[kind]
                obj = (
                    model.objects.filter(
                        uuid=source_uuid,
                        **{relation + "__company_id": user.company_id},
                        is_deleted=False,
                    )
                    .select_related(relation)
                    .first()
                )
                if not obj:
                    unavailable()
                domain = getattr(obj, relation)
                if domain.is_deleted:
                    unavailable()
                run = getattr(domain, run_field)
            if (
                not run
                or run.status != "completed"
                or not WorkflowPermissionService.content_allowed(user, run)
            ):
                unavailable()
            if (
                run.step_runs.filter(step__step_type="approval")
                .exclude(status="skipped")
                .exists()
                and not run.step_runs.filter(approval__status="approved").exists()
            ):
                unavailable()
            docs = list(run.documents.select_related("text_extraction"))
            if kind == "rfp_output":
                ids = {str(d.uuid) for d in docs} | set(obj.data.get("attachments", []))
                responses = [
                    r.get("response_uuid") for r in obj.data.get("responses", [])
                ]
                for response in models.RFPResponseVersion.objects.filter(
                    uuid__in=[r for r in responses if r], item__project=domain
                ):
                    ids.update(p["document_uuid"] for p in response.evidence)
                docs = list(
                    Document.objects.filter(
                        uuid__in=ids, company_id=user.company_id
                    ).select_related("text_extraction")
                )
                if len(docs) != len(ids):
                    unavailable()
            scope = SimpleNamespace(
                scope={"document_uuids": [str(d.uuid) for d in docs]}
            )
            if kind == "compliance_output":
                from apps.workflows.services.compliance_assessment_service import (
                    ComplianceAssessmentService,
                )

                ComplianceAssessmentService.authorize(user, domain, self.action)
                try:
                    ComplianceAssessmentService.readiness(user, domain)
                except ValidationError:
                    current = False
                if obj.run_id != run.pk or obj.evaluation.assessment_id != domain.pk:
                    unavailable()
                version = {
                    "run_uuid": str(run.uuid),
                    "framework_version_uuid": str(domain.framework_version.uuid),
                    "assessment_revision": domain.revision,
                }
            else:
                # Existing typed adapters enforce output revision, approval and
                # transitive domain lineage (including RFP knowledge sources).
                version, current = ComplianceEvidenceService.resolve(
                    user, scope, kind, source_uuid, self.action
                )
            data = obj.data
        if not docs or len(docs) > 100:
            raise ValidationError("Proof source scope exceeds bounds.")
        document_pins = [self.document(d)[0] for d in sorted(docs, key=lambda d: d.pk)]
        lineage_verified = True
        if kind in {"invoice_output", "contract_output"}:
            lineage_verified = bool(obj.source_pins)
            if obj.source_pins and obj.source_pins != document_pins:
                unavailable()
        elif kind == "workflow_artifact":
            lineage_verified = False
        pin = {
            "kind": kind,
            "reference": source_uuid,
            "version": version,
            "digest": digest(data),
            "documents": document_pins,
            "lineage_verified": lineage_verified,
        }
        result = {
            "object": obj,
            "documents": docs,
            "pin": pin,
            "data": data,
            "current": current,
        }
        self.cache[key] = result
        return result


def output_path(data, path):
    if not isinstance(path, list) or not 1 <= len(path) <= 12:
        raise ValidationError("Invalid output path.")
    for part in path:
        if isinstance(data, dict) and isinstance(part, str) and part in data:
            data = data[part]
        elif isinstance(data, list) and type(part) is int and 0 <= part < len(data):
            data = data[part]
        else:
            raise ValidationError("Output field is unavailable.")
    return data
