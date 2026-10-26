import json
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.document_processing.services.document_classification_service import (
    DocumentClassificationService,
)
from apps.documents.services import DocumentPermissionService
from apps.documents.services.field_permission_service import FieldPermissionService
from apps.workflows.models import (
    ContractReview,
    ContractReviewOutput,
    ExtractionCorrection,
    ExtractionResultField,
    WorkflowFinding,
)
from apps.workflows.permissions.workflow_permissions import WorkflowPermissionService
from apps.workflows.services.contract_definition_service import (
    ContractDefinitionService,
    OPERATIONS,
    FINAL_OPERATION,
)
from apps.workflows.services.contract_extraction_service import (
    ContractExtractionService,
    field_specs,
    validate_evidence,
    evidence_chunks,
)
from apps.workflows.services.contract_schema import (
    ContractPolicy,
    ROW_FIELDS,
    assess,
    finding,
    normalize,
)
from apps.workflows.services.workflow_engine_service import (
    WorkflowConflict,
    WorkflowEngineService,
    fingerprint,
)
from apps.workflows.services.workflow_events import workflow_event
from apps.workflows.services.workflow_processing_service import ProcessingResult


class ContractWorkflowService:
    @staticmethod
    def values(review):
        return (
            {
                f.field_key: f.final_value() or None
                for f in review.extraction_run.result_fields.filter(is_deleted=False)
            }
            if review.extraction_run_id
            else {}
        )

    @classmethod
    def refresh(cls, review, policy):
        data, errors = normalize(cls.values(review), policy)
        errors += assess(data, policy)
        fields = (
            list(review.extraction_run.result_fields.filter(is_deleted=False))
            if review.extraction_run_id
            else []
        )
        all_proofs = []
        for field in fields:
            all_proofs.extend(field.source_metadata.get("evidence", []))
            all_proofs.extend(field.source_metadata.get("review_evidence", []))
        for key in ROW_FIELDS:
            for row in data.get(key) or []:
                all_proofs.extend(row.get("evidence", []))
        source_chunks = evidence_chunks(review.document, all_proofs)
        for field in fields:
            field.normalized_value = {"value": data[field.field_key]}
            if field.source_metadata.get("conflict") and not field.is_corrected:
                errors.append(
                    finding(
                        "conflicting_terms",
                        [field.field_key],
                        category="evidence_conflict",
                    )
                )
            if field.final_value() and data.get(field.field_key) not in (None, [], ""):
                proof = (
                    field.source_metadata.get("review_evidence")
                    if field.is_corrected
                    else field.source_metadata.get("evidence")
                )
                validate_evidence(review.document, proof or [], source_chunks)
                if field.field_key in ROW_FIELDS:
                    for row in data[field.field_key]:
                        validate_evidence(
                            review.document,
                            row.get("evidence") or proof or [],
                            source_chunks,
                        )
        if fields:
            ExtractionResultField.objects.bulk_update(fields, ["normalized_value"])
        if not review.coverage.get("complete"):
            errors.append(
                finding("source_coverage_incomplete", category="evidence_conflict")
            )
        if review.classification.get(
            "document_type"
        ) != "contract" or not review.classification.get("document_confidence"):
            errors.append(
                finding(
                    "document_classification_uncertain",
                    ["contract_type"],
                    category="ambiguous_term",
                )
            )
        scores = []
        for key in policy["required_fields"]:
            field = next((f for f in fields if f.field_key == key), None)
            if field and field.is_corrected:
                continue
            score = field.source_metadata.get("extracted_confidence") if field else None
            if score is None:
                scores.append(None)
            else:
                scores.append(Decimal(score))
        review.effective_confidence = (
            None if not scores or None in scores else min(scores)
        )
        if review.effective_confidence is None or review.effective_confidence < Decimal(
            policy["confidence_threshold"]
        ):
            errors.append(
                finding(
                    "low_confidence",
                    policy["required_fields"],
                    category="review_required",
                )
            )
        review.normalized, review.validation = data, errors
        review.classification = {
            **review.classification,
            "contract_type": data["contract_type"],
            "uncertain": data["contract_type"] == "unknown"
            or review.classification.get("document_type") != "contract"
            or not review.classification.get("document_confidence"),
        }
        review.save(
            update_fields=[
                "normalized",
                "validation",
                "classification",
                "effective_confidence",
                "updated_at",
            ]
        )
        return errors

    @staticmethod
    def issue_key(review, item):
        return fingerprint(
            {
                "code": item["code"],
                "fields": item["fields"],
                "revision": review.revision,
            }
        )

    @classmethod
    def findings(cls, review, step_run, errors):
        assigned_review = (
            review.workflow_run.step_runs.filter(step__key="review_if_required").first()
            or step_run
        )
        field_map = (
            {f.field_key: f for f in review.extraction_run.result_fields.all()}
            if review.extraction_run_id
            else {}
        )
        for item in errors:
            key = cls.issue_key(review, item)
            if review.workflow_run.findings.filter(
                data__contract_issue_key=key, is_deleted=False
            ).exists():
                continue
            proof = []
            # Absence findings refer to coverage, not fabricated clause citations.
            for field_key in item["fields"]:
                field = field_map.get(field_key)
                if field and item["category"] not in (
                    "missing_information",
                    "missing_clause",
                ):
                    proof += field.source_metadata.get(
                        "review_evidence", field.source_metadata.get("evidence", [])
                    )
            if item["category"] == "risk" and not proof:
                raise ValidationError("Contract risk findings require fact evidence.")
            WorkflowFinding.objects.create(
                run=review.workflow_run,
                step_run=assigned_review,
                category=item["category"],
                severity=item["severity"],
                title=item["code"].replace("_", " ").title(),
                description="Document analysis identified a policy-based review item; this is not an authoritative legal conclusion.",
                confidence=None,
                evidence=proof[:20],
                data={
                    "contract_issue_key": key,
                    "contract_revision": review.revision,
                    "code": item["code"],
                    "fields": item["fields"],
                    "blocking": item["blocking"],
                    "risk_category": item.get("risk_category"),
                    "source_document_uuid": str(review.document.uuid),
                    "coverage_complete": review.coverage.get("complete", False),
                },
            )
            workflow_event(
                review.workflow_run,
                review.workflow_run.initiated_by,
                "contract_finding_created",
                code=item["code"],
                contract_revision=review.revision,
            )

    @classmethod
    def assert_valid_review(cls, review, policy):
        if policy.get("clause_playbook_version"):
            from apps.workflows.services.clause_analysis_service import (
                ClauseAnalysisService,
            )

            analysis = review.clause_analyses.first()
            if not analysis:
                raise ValidationError("Contract legal clause analysis is required.")
            ClauseAnalysisService.assert_review(analysis)
        errors = cls.refresh(review, policy)
        if any(
            item["code"] == "source_coverage_incomplete"
            or item["code"] == "invalid_field"
            or item["code"].startswith("ambiguous_")
            or item["code"] == "conflicting_terms"
            for item in errors
        ):
            raise ValidationError(
                "Correct invalid or conflicting facts and provide complete source coverage before review acceptance."
            )
        decisions = set(
            review.workflow_run.findings.filter(
                data__contract_revision=review.revision,
                status__in=["accepted", "dismissed", "resolved"],
                review_state="reviewed",
            ).values_list("data__contract_issue_key", flat=True)
        )
        # Missing information can be explicitly acknowledged without inventing it.
        if any(
            item["blocking"] and cls.issue_key(review, item) not in decisions
            for item in errors
        ):
            raise ValidationError(
                "Review current contract findings before accepting the contract."
            )

    @staticmethod
    def provenance(review):
        if not review.extraction_run_id:
            return {}
        return {
            f.field_key: {
                "field_uuid": str(f.uuid),
                "raw_value": f.extracted_value or None,
                "normalized_value": review.normalized.get(f.field_key),
                "corrected_value": f.corrected_value if f.is_corrected else None,
                "confidence": f.source_metadata.get("extracted_confidence"),
                "confidence_basis": f.source_metadata.get("confidence_basis"),
                "review_status": "reviewed" if f.reviewed_by_id else "unreviewed",
                "reviewed_by": f.reviewed_by_id,
                "evidence": f.source_metadata.get(
                    "review_evidence", f.source_metadata.get("evidence", [])
                ),
                "original_evidence": f.source_metadata.get("evidence", []),
            }
            for f in review.extraction_run.result_fields.all()
        }

    @classmethod
    @transaction.atomic
    def correct(
        cls, user, field, corrected_value, expected_revision, reason, proofs=None
    ):
        review = ContractReview.objects.get(extraction_run_id=field.extraction_run_id)
        run = WorkflowEngineService.locked(review.workflow_run)
        review = ContractReview.objects.select_for_update().get(pk=review.pk)
        step = run.step_runs.get(step=run.current_step)
        WorkflowPermissionService.require(user, run, "review", step)
        DocumentPermissionService.require_action(user, review.document, "correct")
        if (
            run.status not in ("review_required", "exception")
            or step.status != "waiting"
        ):
            raise ValidationError(
                "Contract corrections require an open workflow review."
            )
        if not FieldPermissionService.field_allowed(
            user, review.document, field.field_key
        ):
            raise PermissionDenied("Contract field correction is not permitted.")
        if expected_revision != run.revision:
            raise WorkflowConflict()
        if (
            not isinstance(corrected_value, str)
            or len(corrected_value.encode()) > 262144
            or not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > 4000
        ):
            raise ValidationError(
                "Contract corrections require bounded values and a reason."
            )
        field = ExtractionResultField.objects.select_for_update().get(
            pk=field.pk, is_deleted=False
        )
        policy = cls.policy(run)
        data, errors = normalize({field.field_key: corrected_value}, policy)
        if errors:
            raise ValidationError("Correction does not match the contract schema.")
        proofs = (
            proofs if proofs is not None else field.source_metadata.get("evidence", [])
        )
        if corrected_value:
            validate_evidence(review.document, proofs)
            source = review.document.text_extraction.extracted_text
            content = "\n".join(
                chunk_content(review.document, p, source) for p in proofs
            )
            values = [corrected_value]
            if field.field_key in ROW_FIELDS:
                values = [
                    v
                    for row in data[field.field_key]
                    for k, v in row.items()
                    if k
                    not in (
                        "evidence",
                        "role",
                        "category",
                        "timing_type",
                        "kind",
                        "date",
                    )
                    and v is not None
                ]
                for row in data[field.field_key]:
                    row["evidence"] = proofs
                corrected_value = json.dumps(data[field.field_key])
            if any(v not in content for v in values):
                raise ValidationError(
                    "Corrected contract values require matching source evidence."
                )
        review.revision += 1
        ExtractionCorrection.objects.create(
            field=field,
            actor=user,
            revision=review.revision,
            previous_value=field.final_value(),
            corrected_value=corrected_value,
            reason=reason,
            source_metadata={
                "previous_evidence": field.source_metadata.get(
                    "review_evidence", field.source_metadata.get("evidence", [])
                ),
                "evidence": proofs,
            },
        )
        field.corrected_value, field.is_corrected, field.is_missing = (
            corrected_value,
            True,
            not corrected_value,
        )
        field.reviewed_by, field.reviewed_at = user, timezone.now()
        field.source_metadata = {**field.source_metadata, "review_evidence": proofs}
        field.save(
            update_fields=[
                "corrected_value",
                "is_corrected",
                "is_missing",
                "reviewed_by",
                "reviewed_at",
                "source_metadata",
                "updated_at",
            ]
        )
        review.save(update_fields=["revision", "updated_at"])
        errors = cls.refresh(review, policy)
        cls.findings(review, step, errors)
        WorkflowEngineService.set_state(run, run.status, user)
        workflow_event(
            run,
            user,
            "contract_field_corrected",
            field_key=field.field_key,
            contract_revision=review.revision,
        )
        return field

    @staticmethod
    def policy(run):
        step = run.version.steps.filter(
            step_type="processing", configuration__adapter="contract_review"
        ).first()
        return ContractPolicy.validate(step.configuration["options"])

    @classmethod
    @transaction.atomic
    def finalize(cls, review, policy):
        run = WorkflowEngineService.locked(review.workflow_run)
        review = ContractReview.objects.select_for_update().get(pk=review.pk)
        if run.status != "completed" and not (
            run.status == "processing" and run.current_step.key == FINAL_OPERATION
        ):
            raise ValidationError(
                "Contract finalization requires the workflow output boundary."
            )
        ContractExtractionService.authorize(review)
        if not WorkflowPermissionService.content_allowed(run.initiated_by, run):
            raise PermissionDenied("Current contract permissions deny finalization.")
        cls.assert_valid_review(review, policy)
        approval = (
            run.step_runs.filter(
                step__step_type="approval", approval__status="approved"
            )
            .select_related("approval")
            .first()
        )
        if policy["require_approval"] and not approval:
            raise ValidationError("Contract output requires completed approval.")
        existing = review.outputs.filter(revision=review.revision).first()
        if existing:
            return existing.data
        review.finalized_at = timezone.now()
        data = {
            "schema_version": 1,
            "contract": review.normalized,
            "source": {
                "document_uuid": str(review.document.uuid),
                "workflow_uuid": str(run.uuid),
                "extraction_uuid": str(review.extraction_run.uuid),
                "revision": review.revision,
            },
            "field_provenance": cls.provenance(review),
            "coverage": review.coverage,
            "review_status": "reviewed"
            if run.step_runs.filter(
                step__step_type="human_review", status="completed"
            ).exists()
            else "not_required",
            "approval_status": "approved" if approval else "not_required",
            "approval_uuid": str(approval.approval.uuid) if approval else None,
            "finalized_at": review.finalized_at.isoformat(),
            "risks": list(
                run.findings.filter(
                    data__contract_revision=review.revision, category="risk"
                ).values(
                    "category",
                    "severity",
                    "title",
                    "description",
                    "evidence",
                    "data",
                    "status",
                    "review_state",
                )
            ),
            "missing_information": [
                x
                for x in review.validation
                if x["category"] in ("missing_information", "missing_clause")
            ],
            "validation": review.validation,
        }
        from apps.workflows.services.workflow_definition_service import bounded_json

        if policy.get("clause_playbook_version"):
            from apps.workflows.services.clause_analysis_service import (
                ClauseAnalysisService,
            )

            data["clause_intelligence"] = ClauseAnalysisService.finalize(
                review.clause_analyses.get()
            )
        bounded_json(data, 1048576)
        from apps.workflows.services.proof_source_service import ProofSourceService

        source_resolver = ProofSourceService(run.initiated_by)
        output = ContractReviewOutput.objects.create(
            contract_review=review,
            revision=review.revision,
            data=data,
            digest=fingerprint(data),
            source_pins=[source_resolver.document(review.document)[0]],
        )
        review.save(update_fields=["finalized_at", "updated_at"])
        review.extraction_run.status = "approved" if approval else "success"
        review.extraction_run.approval_request_uuid = (
            approval.approval.uuid if approval else None
        )
        review.extraction_run.save(
            update_fields=["status", "approval_request_uuid", "updated_at"]
        )
        workflow_event(
            run,
            run.initiated_by,
            "contract_output_finalized",
            contract_revision=review.revision,
            digest=output.digest,
        )
        return output.data


def chunk_content(document, proof, source):
    if "chunk_uuid" in proof:
        from apps.vector_search.models import DocumentChunk

        return DocumentChunk.objects.get(
            uuid=proof["chunk_uuid"], document=document, is_deleted=False
        ).content
    return source[proof["start"] : proof["end"]]


class ContractProcessingAdapter:
    OPERATIONS = {*OPERATIONS, "prepare_reviewed_output", FINAL_OPERATION}
    validate_version = staticmethod(ContractDefinitionService.validate_version)

    @staticmethod
    def snapshot(operation, options):
        return (
            {"schema_version": 1, "fields": field_specs()}
            if operation == "extract_contract_metadata"
            else {"schema_version": 1}
        )

    @classmethod
    def validate_config(cls, company_id, operation, options):
        if not isinstance(operation, str) or operation not in cls.OPERATIONS:
            raise ValidationError("Unsupported contract processor operation.")
        policy = ContractPolicy.validate(options)
        if policy.get("clause_playbook_version"):
            from apps.workflows.services.clause_definition_service import (
                ClauseProcessingAdapter,
            )

            ClauseProcessingAdapter.validate_config(
                company_id,
                "analyze_clauses",
                {
                    "playbook_version": policy["clause_playbook_version"],
                    "use_ai": policy["use_ai"],
                },
            )

    @staticmethod
    def validate_start(user, documents, options):
        policy = ContractPolicy.validate(options)
        if len(documents) != 1:
            raise ValidationError(
                "Contract review currently requires one principal document."
            )
        DocumentPermissionService.require_action(user, documents[0], "extract")
        if any(
            FieldPermissionService.has_restrictions(user, documents[0], action)
            for action in ("view", "ai")
        ):
            raise PermissionDenied("Current contract field policies deny processing.")
        if policy["use_ai"]:
            DocumentPermissionService.require_action(user, documents[0], "ai")

    @staticmethod
    def validate_command(user, run, action, options):
        review = ContractReview.objects.filter(workflow_run=run).first()
        if review and (
            action in ("accept", "approve", "approved")
            or action == "resolve_exception"
            and run.current_step.key == "review_if_required"
        ):
            ContractWorkflowService.assert_valid_review(
                review, ContractPolicy.validate(options)
            )
            if action == "accept":
                review.extraction_run.result_fields.filter(reviewed_by=None).update(
                    reviewed_by=user, reviewed_at=timezone.now()
                )

    @classmethod
    def execute(cls, run, step_run, operation, options):
        policy = ContractPolicy.validate(options)
        document = run.documents.get()
        review, _ = ContractReview.objects.get_or_create(
            workflow_run=run, defaults={"company": run.company, "document": document}
        )
        ContractExtractionService.authorize(review)
        data = {
            "operation": operation,
            "contract_uuid": str(review.uuid),
            "contract_revision": review.revision,
        }
        if operation == "classify_contract":
            result = DocumentClassificationService.classify(document)
            review.classification = {
                "document_type": result["document_type"],
                "document_confidence": result["confidence"],
                "uncertain": result["document_type"] != "contract",
                "basis": "document_text_keywords",
            }
            review.save(update_fields=["classification", "updated_at"])
        elif operation == "extract_contract_metadata":
            ContractExtractionService.extract(review, step_run, policy)
        elif operation == "assess_confidence":
            errors = ContractWorkflowService.refresh(review, policy)
            ContractWorkflowService.findings(review, step_run, errors)
            data["review_required"] = (
                bool(policy.get("clause_playbook_version"))
                or policy["force_review"]
                or any(x["blocking"] for x in errors)
            )
        elif operation == "prepare_reviewed_output":
            ContractWorkflowService.assert_valid_review(review, policy)
            data["approval_required"] = policy["require_approval"]
            if policy.get("clause_playbook_version"):
                from apps.workflows.services.clause_analysis_service import (
                    ClauseAnalysisService,
                )

                data["approval_required"] |= ClauseAnalysisService.approval_required(
                    review.clause_analyses.get()
                )
        elif operation == "identify_core_clauses" and policy.get(
            "clause_playbook_version"
        ):
            from apps.workflows.services.clause_analysis_service import (
                ClauseAnalysisService,
            )

            ContractWorkflowService.refresh(review, policy)
            analysis = ClauseAnalysisService.analyze(
                run,
                step_run,
                {
                    "playbook_version": policy["clause_playbook_version"],
                    "use_ai": policy["use_ai"],
                },
                review,
            )
            data["clause_analysis_uuid"] = str(analysis.uuid)
        elif operation == FINAL_OPERATION:
            ContractWorkflowService.finalize(review, policy)
            output = review.outputs.get(revision=review.revision)
            data.update(
                {
                    "output_uuid": str(output.uuid),
                    "digest": output.digest,
                    "schema_version": 1,
                }
            )
        elif (
            operation in {"assess_contract_risks", "validate_evidence"}
            and review.extraction_run_id
        ):
            ContractWorkflowService.refresh(review, policy)
        workflow_event(
            run,
            run.initiated_by,
            "contract_step_completed",
            operation=operation,
            contract_revision=review.revision,
        )
        return ProcessingResult(data)
