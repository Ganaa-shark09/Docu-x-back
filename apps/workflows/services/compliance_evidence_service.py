"""Current authorization and reproducible pins for explicitly selected evidence."""

import hashlib

from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.documents.models import Document
from apps.workflows.models import WorkflowArtifact
from apps.workflows.permissions.workflow_permissions import WorkflowPermissionService
from apps.workflows.services.finance_audit_service import FinanceAuditService, digest
from apps.workflows.services.clause_detection_service import ClauseDetectionService
from apps.workflows.services import compliance_schema as schema


class ComplianceEvidenceService:
    @staticmethod
    def documents(user, scope, action="view"):
        ids = scope["document_uuids"]
        docs = list(
            Document.objects.filter(uuid__in=ids, company_id=user.company_id)
            .select_related("text_extraction")
            .order_by("pk")
        )
        if len(docs) != len(ids):
            raise PermissionDenied(
                "The explicit compliance source scope is unavailable."
            )
        for doc in docs:
            FinanceAuditService.document(user, doc, action)
        return docs

    @staticmethod
    def document_pin(user, doc, action):
        FinanceAuditService.document(user, doc, action)
        text, coverage = ClauseDetectionService.source(doc)
        return {
            "uuid": str(doc.uuid),
            "governance_revision": doc.governance_revision,
            "checksum": doc.checksum,
            "file_digest": hashlib.sha256(FinanceAuditService.raw(doc)).hexdigest(),
            "text_digest": digest(text),
            "complete": coverage["complete"],
            "legal_hold": bool(getattr(doc, "legal_hold", False)),
        }

    @classmethod
    def resolve(cls, user, assessment, kind, source_uuid, action="view"):
        schema.choice(kind, schema.SOURCE_KINDS)
        source_uuid = schema.uuid(source_uuid)
        extra = {}
        approved = False
        current = True
        if kind == "document":
            obj = Document.objects.filter(
                uuid=source_uuid, company_id=user.company_id
            ).first()
            if not obj:
                raise PermissionDenied("Evidence source is unavailable.")
            docs = [obj]
            data = {}
        elif kind == "hr_policy_version":
            from apps.workflows.models import HRPolicyVersion
            from apps.workflows.services.hr_policy_service import (
                HRPolicyService,
                effective_status,
            )

            obj = (
                HRPolicyVersion.objects.filter(
                    uuid=source_uuid, policy__company_id=user.company_id
                )
                .select_related("publication", "policy", "document")
                .first()
            )
            if not obj:
                raise PermissionDenied("Policy evidence is unavailable.")
            HRPolicyService.authorize(user, obj, action)
            if (
                obj.workflow_run.status != "completed"
                or not obj.workflow_run.step_runs.filter(
                    approval__status="approved"
                ).exists()
            ):
                raise PermissionDenied(
                    "Published policy workflow approval is unavailable."
                )
            docs = [obj.document]
            current = effective_status(obj) == "effective"
            approved = True
            data = {"version": obj.number, "source_digest": obj.source_digest}
            extra = {"policy_uuid": str(obj.policy.uuid), "version": obj.number}
        else:
            from apps.workflows.models import (
                ContractReviewOutput,
                ClauseAnalysisOutput,
                FinanceAuditOutput,
                RFPProposalOutput,
                InvoiceOutputVersion,
            )

            registry = {
                "contract_output": (
                    ContractReviewOutput,
                    "contract_review",
                    "company_id",
                    "workflow_run",
                ),
                "clause_output": (
                    ClauseAnalysisOutput,
                    "analysis",
                    "company_id",
                    "workflow_run",
                ),
                "finance_output": (FinanceAuditOutput, "audit", "company_id", "run"),
                "rfp_output": (RFPProposalOutput, "project", "company_id", "run"),
                "invoice_output": (
                    InvoiceOutputVersion,
                    "invoice",
                    "company_id",
                    "workflow_run",
                ),
            }
            if kind == "workflow_artifact":
                obj = (
                    WorkflowArtifact.objects.filter(
                        uuid=source_uuid,
                        run__company_id=user.company_id,
                        is_deleted=False,
                    )
                    .select_related("run__version__definition")
                    .first()
                )
                if not obj or obj.run.version.definition.workflow_type in {
                    "compliance",
                    "finance_audit",
                    "rfp_proposal",
                    "hr_policy",
                    "legal_clause_intelligence",
                    "contract_review",
                    "invoice",
                }:
                    raise PermissionDenied(
                        "Domain evidence requires its typed versioned output reference."
                    )
                run = obj.run
            else:
                model, relation, company_field, run_field = registry[kind]
                obj = (
                    model.objects.filter(
                        uuid=source_uuid,
                        **{relation + "__" + company_field: user.company_id},
                        is_deleted=False,
                    )
                    .select_related(relation)
                    .first()
                )
                if not obj:
                    raise PermissionDenied("Approved workflow evidence is unavailable.")
                domain = getattr(obj, relation)
                if domain.is_deleted:
                    raise PermissionDenied("Workflow evidence is unavailable.")
                run = getattr(domain, run_field)
                if hasattr(obj, "run_id") and obj.run_id != run.pk:
                    raise PermissionDenied(
                        "The source output belongs to a different workflow revision."
                    )
                if kind == "finance_output":
                    FinanceAuditService.authorize(user, domain, action)
                elif kind == "clause_output":
                    from apps.workflows.services.clause_analysis_service import (
                        ClauseAnalysisService,
                    )

                    ClauseAnalysisService.authorize(user, domain)
                elif kind == "rfp_output":
                    from apps.workflows.services.rfp_service import RFPService

                    RFPService.export(user, domain, obj, external=False)
                    from apps.workflows.models import RFPResponseVersion

                    response_ids = [
                        r["response_uuid"]
                        for r in obj.data.get("responses", [])
                        if r.get("response_uuid")
                    ]
                    supporting = set(obj.data.get("attachments", []))
                    for response in RFPResponseVersion.objects.filter(
                        uuid__in=response_ids, item__project=domain
                    ):
                        supporting.update(
                            proof["document_uuid"] for proof in response.evidence
                        )
                    extra["supporting_document_uuids"] = sorted(supporting)
                elif getattr(obj, "revision", None) != getattr(
                    domain, "revision", None
                ):
                    raise PermissionDenied("Workflow evidence version is stale.")
                extra = {
                    **extra,
                    "domain_uuid": str(domain.uuid),
                    "revision": getattr(domain, "revision", None),
                }
            if (
                not run
                or run.status != "completed"
                or not WorkflowPermissionService.content_allowed(user, run)
            ):
                raise PermissionDenied(
                    "A completed authorized source workflow is required."
                )
            approved = run.step_runs.filter(approval__status="approved").exists()
            if (
                not approved
                and run.step_runs.filter(step__step_type="approval")
                .exclude(status="skipped")
                .exists()
            ):
                raise PermissionDenied("Source workflow approval is required.")
            docs = list(run.documents.select_related("text_extraction"))
            if kind == "rfp_output":
                docs = list(
                    {
                        d.pk: d
                        for d in docs
                        + list(
                            Document.objects.filter(
                                company=user.company,
                                uuid__in=extra["supporting_document_uuids"],
                            ).select_related("text_extraction")
                        )
                    }.values()
                )
            if not docs or len(docs) > 100:
                raise ValidationError(
                    "Workflow source package exceeds bounded evidence scope."
                )
            data = obj.data
            extra["run_uuid"] = str(run.uuid)
        if obj.is_deleted:
            raise PermissionDenied("Evidence source is unavailable.")
        scope = set(assessment.scope["document_uuids"])
        if any(str(doc.uuid) not in scope for doc in docs):
            raise PermissionDenied(
                "Evidence must be inside the explicit authorized document scope."
            )
        pin = {
            "source_uuid": source_uuid,
            "source_kind": kind,
            "digest": digest(data),
            "approved": approved,
            "documents": [
                cls.document_pin(user, doc, action)
                for doc in sorted(docs, key=lambda d: d.pk)
            ],
            **extra,
        }
        return pin, current

    @classmethod
    def authorize(cls, user, assessment, action="view"):
        cls.documents(user, assessment.scope, action)
        resolved = {}
        for link in assessment.evidence.order_by("pk"):
            key = (link.source_kind, link.source_uuid)
            if key not in resolved:
                resolved[key] = cls.resolve(user, assessment, *key, action)
            pin, _ = resolved[key]
            if pin != link.pin:
                raise PermissionDenied(
                    "Pinned compliance evidence changed or was revoked."
                )

    @classmethod
    def validity(cls, user, assessment, link, reviewed=False, resolved=None):
        pin, current = (
            resolved
            if resolved is not None
            else cls.resolve(user, assessment, link.source_kind, link.source_uuid)
        )
        if pin != link.pin:
            return "revoked"
        today = timezone.localdate().isoformat()
        if link.dates.get("expires", "9999-12-31") <= today:
            return "expired"
        if not current or link.dates.get("review_due", "9999-12-31") < today:
            return "stale"
        if (
            link.dates.get("effective", "0001-01-01") > today
            or link.dates.get("issued", "0001-01-01") > today
        ):
            return "insufficient"
        if (
            not all(d["complete"] for d in pin["documents"])
            and link.source_kind == "document"
        ):
            return "insufficient"
        return "valid" if reviewed else "pending_review"

    @classmethod
    def discover(cls, user, assessment, query):
        # Exact scoped discovery is intentionally deterministic: source text is data,
        # never an instruction, executable expression, model prompt or certification.
        query = schema.text(query, 200)
        results = []
        for doc in cls.documents(user, assessment.scope):
            text, coverage = ClauseDetectionService.source(doc)
            if not coverage["complete"]:
                continue
            offset = text.casefold().find(query.casefold())
            if offset >= 0:
                results.append(
                    {
                        "document_uuid": str(doc.uuid),
                        "start": offset,
                        "end": offset + len(query),
                        "quote": text[offset : offset + len(query)],
                        "status": "pending_review",
                    }
                )
            if len(results) == 50:
                break
        return {
            "method": "scoped_exact_search",
            "candidates": results,
            "advisory_only": True,
        }
