import json
import logging
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
    ExtractionCorrection,
    ExtractionResultField,
    InvoiceOutputVersion,
    InvoiceRecord,
    WorkflowFinding,
)
from apps.workflows.permissions.workflow_permissions import WorkflowPermissionService
from apps.workflows.services.invoice_extraction_service import InvoiceExtractionService
from apps.workflows.services.invoice_matching_service import InvoiceMatchingService
from apps.workflows.services.invoice_schema import (
    InvoicePolicy,
    issue,
    normalize,
    validate_fields,
    validate_financials,
)
from apps.workflows.services.workflow_engine_service import (
    WorkflowConflict,
    WorkflowEngineService,
    fingerprint,
)
from apps.workflows.services.workflow_events import workflow_event
from apps.workflows.services.workflow_processing_service import ProcessingResult


logger = logging.getLogger(__name__)


class InvoiceWorkflowService:
    @staticmethod
    def values(invoice):
        result = {}
        if invoice.extraction_run_id:
            for field in invoice.extraction_run.result_fields.filter(is_deleted=False):
                value = field.final_value()
                if field.field_key == "line_items" and value:
                    try:
                        value = json.loads(value)
                    except (ValueError, RecursionError):
                        value = "invalid_line_items"
                result[field.field_key] = value or None
        return result

    @classmethod
    def normalize(cls, invoice, policy):
        data, errors = normalize(cls.values(invoice), policy)
        invoice.normalized = data
        index_number = data["invoice_number"] or ""
        invoice.invoice_number = index_number if len(index_number) <= 150 else ""
        identity = (
            data["vendor_identifier"]
            or data["vendor_tax_id"]
            or (data["vendor_name"] or "").casefold()
        )
        invoice.vendor_key = identity if len(identity) <= 255 else ""
        invoice.invoice_date = data["invoice_date"]
        required = [
            data[k]
            for k in ("invoice_number", "invoice_date", "grand_total", "currency")
        ]
        invoice.fingerprint = (
            fingerprint(
                {
                    "number": data["invoice_number"],
                    "date": data["invoice_date"],
                    "total": str(Decimal(data["grand_total"]).normalize())
                    if data["grand_total"]
                    else None,
                    "currency": data["currency"],
                    "vendor": identity,
                }
            )
            if all(required) and identity
            else ""
        )
        invoice.save(
            update_fields=[
                "normalized",
                "invoice_number",
                "vendor_key",
                "invoice_date",
                "fingerprint",
                "updated_at",
            ]
        )
        if invoice.extraction_run_id:
            fields = list(invoice.extraction_run.result_fields.all())
            for field in fields:
                field.normalized_value = {"value": data.get(field.field_key)}
            ExtractionResultField.objects.bulk_update(fields, ["normalized_value"])
        return errors

    @staticmethod
    def confidence(invoice, policy):
        fields = (
            {f.field_key: f for f in invoice.extraction_run.result_fields.all()}
            if invoice.extraction_run_id
            else {}
        )
        scores = []
        for key in policy["required_fields"]:
            field = fields.get(key)
            if not field or not field.source_metadata.get("extracted_confidence"):
                if not field or not field.is_corrected:
                    return None
                continue
            scores.append(Decimal(field.source_metadata["extracted_confidence"]))
        return min(scores) if scores else None

    @classmethod
    def core_validation(cls, invoice, policy):
        return (
            cls.normalize(invoice, policy)
            + validate_fields(invoice.normalized, policy)
            + validate_financials(invoice.normalized, policy)
        )

    @staticmethod
    def matching_issues(invoice, policy):
        results = []
        if invoice.duplicates:
            results.append(
                issue("duplicate_candidate", [], "high", policy["review_duplicates"])
            )
        vendor = invoice.matches.get("vendor", {})
        if vendor.get("status") != "matched":
            results.append(
                issue(
                    "vendor_ambiguous"
                    if vendor.get("status") == "ambiguous"
                    else "vendor_unmatched",
                    ["vendor_name"],
                    "medium",
                    policy["require_vendor"],
                )
            )
        po = invoice.matches.get("purchase_order", {})
        if policy["require_po"] and po.get("status") != "matched":
            results.append(issue("po_unmatched", ["purchase_order_number"]))
        for code in po.get("checks", []):
            results.append(
                issue(code, ["purchase_order_number", "currency", "grand_total"])
            )
        return results

    @staticmethod
    def findings(invoice, step_run, errors):
        # Findings use the real review step's assignment, not a second review system.
        review = (
            invoice.workflow_run.step_runs.filter(
                step__key="review_if_required"
            ).first()
            or step_run
        )
        for item in errors:
            key = fingerprint(
                {
                    "code": item["code"],
                    "fields": item["fields"],
                    "revision": invoice.revision,
                }
            )
            if invoice.workflow_run.findings.filter(
                data__invoice_issue_key=key, is_deleted=False
            ).exists():
                continue
            WorkflowFinding.objects.create(
                run=invoice.workflow_run,
                step_run=review,
                category=item["code"],
                severity=item["severity"],
                title=item["code"].replace("_", " ").title(),
                description="Invoice processing identified an item requiring policy-based assessment.",
                confidence=None,
                evidence=[{"document_uuid": str(invoice.document.uuid)}],
                data={
                    "invoice_issue_key": key,
                    "invoice_revision": invoice.revision,
                    "fields": item["fields"],
                    "blocking": item["blocking"],
                },
            )
            workflow_event(
                invoice.workflow_run,
                invoice.workflow_run.initiated_by,
                "invoice_validation_finding",
                category=item["code"],
                invoice_revision=invoice.revision,
            )

    @classmethod
    def assessment(cls, invoice, step_run, policy):
        invoice.effective_confidence = cls.confidence(invoice, policy)
        errors = [*invoice.validation, *cls.matching_issues(invoice, policy)]
        if (
            invoice.effective_confidence is None
            or invoice.effective_confidence < Decimal(policy["confidence_threshold"])
        ):
            errors.append(issue("low_confidence", policy["required_fields"], "medium"))
        if invoice.classification.get("uncertain"):
            errors.append(issue("classification_uncertain", [], "medium"))
        invoice.validation = errors
        invoice.save(update_fields=["effective_confidence", "validation", "updated_at"])
        cls.findings(invoice, step_run, errors)
        return {
            "review_required": policy["force_review"]
            or any(x["blocking"] for x in errors),
            "approval_required": policy["require_approval"],
        }

    @classmethod
    def assert_valid_review(cls, invoice, policy):
        errors = cls.core_validation(invoice, policy) + cls.matching_issues(
            invoice, policy
        )
        # Missing/invalid fields must be corrected, not waived into fabricated output.
        hard = [
            x
            for x in errors
            if x["code"].startswith(("missing_", "invalid_", "ambiguous_"))
        ]
        if hard:
            raise ValidationError(
                "Correct missing, invalid or ambiguous invoice fields before accepting review."
            )
        decisions = set(
            invoice.workflow_run.findings.filter(
                data__invoice_revision=invoice.revision,
                status__in=["accepted", "dismissed", "resolved"],
                review_state="reviewed",
            ).values_list("data__invoice_issue_key", flat=True)
        )
        financial = [
            x
            for x in errors
            if x["blocking"]
            and fingerprint(
                {"code": x["code"], "fields": x["fields"], "revision": invoice.revision}
            )
            not in decisions
        ]
        if financial:
            raise ValidationError(
                "Correct financial discrepancies or explicitly review their findings before accepting."
            )
        invoice.validation = errors
        invoice.save(update_fields=["validation", "updated_at"])

    @classmethod
    @transaction.atomic
    def correct(cls, user, field, corrected_value, expected_revision, reason):
        invoice = InvoiceRecord.objects.select_related("workflow_run").get(
            extraction_run_id=field.extraction_run_id
        )
        run = WorkflowEngineService.locked(invoice.workflow_run)
        invoice = InvoiceRecord.objects.select_for_update().get(pk=invoice.pk)
        step = run.step_runs.get(step=run.current_step)
        WorkflowPermissionService.require(user, run, "review", step)
        DocumentPermissionService.require_action(user, invoice.document, "correct")
        if (
            run.status not in {"review_required", "exception"}
            or step.status != "waiting"
        ):
            raise ValidationError(
                "Invoice corrections require an open review or exception."
            )
        if not FieldPermissionService.field_allowed(
            user, invoice.document, field.field_key
        ):
            raise PermissionDenied("Invoice field correction is not permitted.")
        if expected_revision != run.revision:
            raise WorkflowConflict()
        if (
            not isinstance(corrected_value, str)
            or len(corrected_value.encode()) > 262144
        ):
            raise ValidationError("Invoice correction exceeds the supported size.")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 4000:
            raise ValidationError("Invoice corrections require a bounded reason.")
        field = ExtractionResultField.objects.select_for_update().get(
            pk=field.pk, is_deleted=False
        )
        from apps.workflows.services.workflow_processing_service import (
            WorkflowProcessingService,
        )

        _, policy = next(
            (adapter, options)
            for adapter, options in WorkflowProcessingService.processors(run.version)
            if adapter is InvoiceProcessingAdapter
        )
        policy = InvoicePolicy.validate(policy)
        try:
            value = (
                json.loads(corrected_value)
                if field.field_key == "line_items" and corrected_value
                else corrected_value
            )
        except (ValueError, RecursionError):
            raise ValidationError(
                "Corrected line items must match the invoice JSON schema."
            )
        _, errors = normalize({field.field_key: value}, policy)
        if errors:
            raise ValidationError(
                "Correction does not satisfy the invoice field schema."
            )
        invoice.revision += 1
        ExtractionCorrection.objects.create(
            field=field,
            actor=user,
            revision=invoice.revision,
            previous_value=field.final_value(),
            corrected_value=corrected_value,
            reason=reason,
        )
        field.corrected_value = corrected_value
        field.is_corrected = True
        field.is_missing = not corrected_value.strip()
        field.reviewed_by = user
        field.reviewed_at = timezone.now()
        field.save(
            update_fields=[
                "corrected_value",
                "is_corrected",
                "is_missing",
                "reviewed_by",
                "reviewed_at",
                "updated_at",
            ]
        )
        invoice.save(update_fields=["revision", "updated_at"])
        invoice.validation = cls.core_validation(invoice, policy)
        invoice.matches["vendor"] = InvoiceMatchingService.vendor(invoice)
        invoice.matches["purchase_order"] = InvoiceMatchingService.purchase_order(
            invoice, user, policy
        )
        invoice.duplicates = InvoiceMatchingService.duplicates(invoice, user)
        invoice.validation += cls.matching_issues(invoice, policy)
        invoice.save(
            update_fields=["validation", "matches", "duplicates", "updated_at"]
        )
        cls.findings(invoice, step, invoice.validation)
        WorkflowEngineService.set_state(run, run.status, user)
        workflow_event(
            run,
            user,
            "invoice_field_corrected",
            field_key=field.field_key,
            invoice_revision=invoice.revision,
        )
        return field

    @classmethod
    def provenance(cls, invoice):
        result = {}
        if not invoice.extraction_run_id:
            return result
        for field in invoice.extraction_run.result_fields.all():
            result[field.field_key] = {
                "field_uuid": str(field.uuid),
                "raw_value": field.extracted_value or None,
                "normalized_value": invoice.normalized.get(field.field_key),
                "corrected_value": field.corrected_value
                if field.is_corrected
                else None,
                "extracted_confidence": field.source_metadata.get(
                    "extracted_confidence"
                ),
                "confidence_basis": field.source_metadata.get("confidence_basis"),
                "validation_confidence": None,
                "validation_status": "invalid"
                if any(field.field_key in item["fields"] for item in invoice.validation)
                else "valid"
                if invoice.normalized.get(field.field_key) is not None
                else "unknown",
                "review_status": "reviewed" if field.reviewed_by_id else "unreviewed",
                "reviewed_by": field.reviewed_by_id,
                "reviewed_at": field.reviewed_at.isoformat()
                if field.reviewed_at
                else None,
                "document_uuid": str(invoice.document.uuid),
                "extraction_method": field.source_metadata.get("method"),
                "evidence": field.source_metadata.get("evidence", []),
            }
            if field.field_key == "line_items":
                raw_rows = (
                    json.loads(field.extracted_value) if field.extracted_value else []
                )
                proof = field.source_metadata.get("evidence", [])
                result[field.field_key]["cells"] = [
                    {
                        key: {
                            "raw_value": raw_rows[i].get(key)
                            if i < len(raw_rows)
                            else None,
                            "normalized_value": value,
                            "evidence": proof[i : i + 1]
                            if field.source_metadata.get("method") == "table_header"
                            else proof,
                            "review_status": result[field.field_key]["review_status"],
                        }
                        for key, value in row.items()
                    }
                    for i, row in enumerate(invoice.normalized.get("line_items") or [])
                ]
        return result

    @classmethod
    @transaction.atomic
    def finalize(cls, invoice, policy):
        run = WorkflowEngineService.locked(invoice.workflow_run)
        invoice = InvoiceRecord.objects.select_for_update().get(pk=invoice.pk)
        if run.status != "completed" and not (
            run.status == "processing"
            and run.current_step.key == "generate_normalized_invoice"
        ):
            raise ValidationError(
                "Invoice finalization requires the workflow output boundary."
            )
        DocumentPermissionService.require_action(
            run.initiated_by, invoice.document, "extract"
        )
        if not WorkflowPermissionService.content_allowed(run.initiated_by, run):
            raise PermissionDenied("Current invoice permissions deny finalization.")
        cls.assert_valid_review(invoice, policy)
        approval = (
            run.step_runs.filter(
                step__step_type="approval", approval__status="approved"
            )
            .select_related("approval")
            .first()
        )
        if policy["require_approval"] and not approval:
            raise ValidationError("Invoice output requires completed approval.")
        data = {
            "schema_version": 1,
            "invoice": invoice.normalized,
            "source": {
                "document_uuid": str(invoice.document.uuid),
                "workflow_uuid": str(run.uuid),
                "extraction_uuid": str(invoice.extraction_run.uuid),
                "revision": invoice.revision,
            },
            "vendor": invoice.matches.get("vendor", {}),
            "purchase_order_reference": invoice.matches.get("purchase_order", {}),
            "validation": invoice.validation,
            "field_provenance": cls.provenance(invoice),
            "approval_uuid": str(approval.approval.uuid) if approval else None,
        }
        from apps.workflows.services.workflow_definition_service import bounded_json

        bounded_json(data, 1048576)
        from apps.workflows.services.proof_source_service import ProofSourceService

        source_resolver = ProofSourceService(run.initiated_by)
        output, created = InvoiceOutputVersion.objects.get_or_create(
            invoice=invoice,
            revision=invoice.revision,
            defaults={
                "data": data,
                "digest": fingerprint(data),
                "source_pins": [source_resolver.document(invoice.document)[0]],
            },
        )
        if created:
            invoice.extraction_run.status = "approved" if approval else "success"
            invoice.extraction_run.approval_request_uuid = (
                approval.approval.uuid if approval else None
            )
            invoice.extraction_run.save(
                update_fields=["status", "approval_request_uuid", "updated_at"]
            )
            workflow_event(
                run,
                run.initiated_by,
                "invoice_output_finalized",
                invoice_revision=invoice.revision,
                digest=output.digest,
            )
        return output.data


class InvoiceProcessingAdapter:
    OPERATIONS = {
        "validate_document",
        "classify_invoice",
        "extract_invoice_fields",
        "normalize_fields",
        "validate_fields",
        "validate_totals",
        "detect_duplicate",
        "match_vendor",
        "match_purchase_order",
        "assess_confidence",
        "prepare_reviewed_output",
        "generate_normalized_invoice",
    }

    @staticmethod
    def snapshot(operation, options):
        from apps.workflows.services.invoice_extraction_service import field_specs

        return (
            {"schema_version": 1, "fields": field_specs()}
            if operation == "extract_invoice_fields"
            else {"schema_version": 1}
        )

    @classmethod
    def validate_config(cls, company_id, operation, options):
        if not isinstance(operation, str) or operation not in cls.OPERATIONS:
            raise ValidationError("Unsupported invoice processor operation.")
        InvoicePolicy.validate(options)

    @classmethod
    def validate_version(cls, version, options):
        steps = list(
            version.steps.filter(
                step_type="processing", configuration__adapter="invoice"
            )
        )
        policy = InvoicePolicy.validate(options)
        if any(
            InvoicePolicy.validate(step.configuration.get("options", {})) != policy
            for step in steps
        ):
            raise ValidationError(
                "All invoice steps must share one frozen invoice policy."
            )
        operations = [step.configuration["operation"] for step in steps]
        if set(operations) != cls.OPERATIONS or len(operations) != len(cls.OPERATIONS):
            raise ValidationError(
                "Invoice workflows require the complete, unique invoice lifecycle."
            )
        chain = [
            "validate_document",
            "classify_invoice",
            "extract_invoice_fields",
            "normalize_fields",
            "validate_fields",
            "validate_totals",
            "detect_duplicate",
            "match_vendor",
            "match_purchase_order",
            "assess_confidence",
        ]
        expected = {(a, b, "success") for a, b in zip(chain, chain[1:])}
        expected.update(
            {
                ("extract_invoice_fields", "normalize_fields", "exception"),
                ("assess_confidence", "review_decision", "success"),
                ("review_decision", "review_if_required", "pass"),
                ("review_decision", "prepare_reviewed_output", "fail"),
                ("review_if_required", "prepare_reviewed_output", "accepted"),
                ("review_if_required", "prepare_reviewed_output", "exception"),
                ("review_if_required", "rejected", "rejected"),
                ("prepare_reviewed_output", "approval_decision", "success"),
                ("approval_decision", "approve_if_required", "pass"),
                ("approval_decision", "generate_normalized_invoice", "fail"),
                ("approve_if_required", "generate_normalized_invoice", "approved"),
                ("approve_if_required", "rejected", "rejected"),
                ("generate_normalized_invoice", "completed", "success"),
            }
        )
        actual = set(
            version.transitions.values_list("source__key", "target__key", "outcome")
        )
        if (
            version.entry_step != chain[0]
            or actual != expected
            or any(step.key != step.configuration["operation"] for step in steps)
        ):
            raise ValidationError(
                "Invoice graph must preserve validation, review and approval boundaries."
            )
        all_steps = {step.key: step for step in version.steps.all()}
        additional = {
            "review_decision": "rule",
            "approval_decision": "rule",
            "review_if_required": "human_review",
            "approve_if_required": "approval",
            "completed": "terminal",
            "rejected": "terminal",
        }
        if set(all_steps) != cls.OPERATIONS | set(additional) or any(
            all_steps[key].step_type != kind for key, kind in additional.items()
        ):
            raise ValidationError(
                "Invoice graph step types do not match the lifecycle."
            )
        for key, source, path in [
            ("review_decision", "assess_confidence", "review_required"),
            ("approval_decision", "prepare_reviewed_output", "approval_required"),
        ]:
            if all_steps[key].configuration != {
                "source_step": source,
                "path": path,
                "operator": "eq",
                "value": True,
            }:
                raise ValidationError(
                    "Invoice review and approval rules must use computed policy decisions."
                )
        if (
            all_steps["completed"].configuration.get("status", "completed")
            != "completed"
            or all_steps["rejected"].configuration.get("status") != "rejected"
        ):
            raise ValidationError("Invoice terminal outcomes are invalid.")

    @staticmethod
    def validate_start(user, documents, options):
        policy = InvoicePolicy.validate(options)
        if len(documents) != 1:
            raise ValidationError(
                "Invoice workflows require one source invoice document."
            )
        document = documents[0]
        DocumentPermissionService.require_action(user, document, "extract")
        if FieldPermissionService.has_restrictions(
            user, document, "ai"
        ) or FieldPermissionService.has_restrictions(user, document, "view"):
            raise PermissionDenied("Current invoice field permissions deny extraction.")
        if policy["use_ai"]:
            DocumentPermissionService.require_action(user, document, "ai")

    @staticmethod
    def validate_command(user, run, action, options):
        resolve_review = (
            action == "resolve_exception"
            and run.current_step.key == "review_if_required"
        )
        if action in {"accept", "approve", "approved"} or resolve_review:
            invoice = InvoiceRecord.objects.filter(workflow_run=run).first()
            if invoice:
                InvoiceWorkflowService.assert_valid_review(
                    invoice, InvoicePolicy.validate(options)
                )
                if action == "accept" or resolve_review:
                    invoice.extraction_run.result_fields.filter(
                        reviewed_by=None
                    ).update(reviewed_by=user, reviewed_at=timezone.now())

    @classmethod
    def execute(cls, run, step_run, operation, options):
        policy = InvoicePolicy.validate(options)
        document = run.documents.get()
        DocumentPermissionService.require_action(run.initiated_by, document, "extract")
        if FieldPermissionService.has_restrictions(run.initiated_by, document, "ai"):
            raise PermissionDenied("Invoice field policies deny processing.")
        invoice, _ = InvoiceRecord.objects.get_or_create(
            workflow_run=run, defaults={"company": run.company, "document": document}
        )
        data = {
            "operation": operation,
            "invoice_uuid": str(invoice.uuid),
            "invoice_revision": invoice.revision,
        }
        errors = []
        if operation == "validate_document":
            if document.status != "ready":
                raise ValidationError(
                    "Source invoice must finish document processing first."
                )
        elif operation == "classify_invoice":
            result = DocumentClassificationService.classify(document)
            if result["confidence"] > 0 and result["document_type"] != "invoice":
                raise ValidationError("Document classification is not an invoice.")
            invoice.classification = {
                "document_type": result["document_type"],
                "confidence": result["confidence"],
                "uncertain": result["confidence"] < 40,
            }
            invoice.save(update_fields=["classification", "updated_at"])
        elif operation == "extract_invoice_fields":
            InvoiceExtractionService.extract(invoice, step_run, policy)
            if (
                not getattr(document, "text_extraction", None)
                or not document.text_extraction.extracted_text.strip()
            ):
                return ProcessingResult(data, exception_category="extraction_failed")
            workflow_event(
                run,
                run.initiated_by,
                "invoice_extraction_completed",
                invoice_revision=invoice.revision,
            )
        elif operation == "normalize_fields":
            invoice.validation = InvoiceWorkflowService.normalize(invoice, policy)
            invoice.save(update_fields=["validation", "updated_at"])
        elif operation == "validate_fields":
            errors = validate_fields(invoice.normalized, policy)
        elif operation == "validate_totals":
            errors = validate_financials(invoice.normalized, policy)
        elif operation == "detect_duplicate":
            invoice.duplicates = InvoiceMatchingService.duplicates(
                invoice, run.initiated_by
            )
            invoice.save(update_fields=["duplicates", "updated_at"])
            workflow_event(
                run,
                run.initiated_by,
                "invoice_duplicate_checked",
                candidate_count=len(invoice.duplicates),
            )
        elif operation == "match_vendor":
            invoice.matches["vendor"] = InvoiceMatchingService.vendor(invoice)
            invoice.save(update_fields=["matches", "updated_at"])
            workflow_event(
                run,
                run.initiated_by,
                "invoice_vendor_matched",
                match_status=invoice.matches["vendor"]["status"],
            )
        elif operation == "match_purchase_order":
            invoice.matches["purchase_order"] = InvoiceMatchingService.purchase_order(
                invoice, run.initiated_by, policy
            )
            invoice.save(update_fields=["matches", "updated_at"])
            workflow_event(
                run,
                run.initiated_by,
                "invoice_po_matched",
                match_status=invoice.matches["purchase_order"]["status"],
            )
        elif operation == "assess_confidence":
            data.update(InvoiceWorkflowService.assessment(invoice, step_run, policy))
        elif operation == "prepare_reviewed_output":
            invoice.matches["vendor"] = InvoiceMatchingService.vendor(invoice)
            invoice.matches["purchase_order"] = InvoiceMatchingService.purchase_order(
                invoice, run.initiated_by, policy
            )
            invoice.duplicates = InvoiceMatchingService.duplicates(
                invoice, run.initiated_by
            )
            invoice.save(update_fields=["matches", "duplicates", "updated_at"])
            InvoiceWorkflowService.assert_valid_review(invoice, policy)
            data["approval_required"] = policy["require_approval"]
        elif operation == "generate_normalized_invoice":
            InvoiceWorkflowService.finalize(invoice, policy)
            output = invoice.outputs.get(revision=invoice.revision)
            data.update(
                {
                    "output_uuid": str(output.uuid),
                    "schema_version": output.schema_version,
                    "digest": output.digest,
                }
            )
        if errors:
            invoice.validation += errors
            invoice.save(update_fields=["validation", "updated_at"])
        logger.info(
            "invoice_step",
            extra={
                "workflow_uuid": str(run.uuid),
                "invoice_uuid": str(invoice.uuid),
                "operation": operation,
                "invoice_revision": invoice.revision,
            },
        )
        return ProcessingResult(data, [{"document_uuid": str(document.uuid)}])
