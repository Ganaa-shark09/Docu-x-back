from apps.approvals.models import ApprovalRequest
from apps.approvals.services import ApprovalWorkflowService
from apps.workflows.models import ExtractionRun
from django.db import transaction
from rest_framework.exceptions import ValidationError
from apps.documents.services import DocumentPermissionService


class ExtractionApprovalService:
    @classmethod
    @transaction.atomic
    def submit_for_approval(
        cls,
        user,
        extraction_run,
        assigned_to=None,
        ip_address=None,
        user_agent="",
    ):
        extraction_run = ExtractionRun.objects.select_for_update().get(
            pk=extraction_run.pk, company=user.company, is_deleted=False
        )
        from apps.workflows.models import ContractReview

        if ContractReview.objects.filter(extraction_run=extraction_run).exists():
            raise ValidationError(
                "Contract reviews use their workflow approval boundary."
            )
        DocumentPermissionService.require_action(
            user, extraction_run.document, "workflow"
        )
        if extraction_run.status not in {
            ExtractionRun.Status.SUCCESS,
            ExtractionRun.Status.REVIEW_REQUIRED,
        }:
            raise ValidationError(
                "Only completed extraction in review can be submitted."
            )
        submitted_payload = {
            field.field_key: {
                "label": field.label,
                "value": field.final_value(),
                "confidence": float(field.confidence_score),
                "is_corrected": field.is_corrected,
                "source_text": field.source_text,
            }
            for field in extraction_run.result_fields.filter(is_deleted=False)
        }

        approval = ApprovalWorkflowService.create_approval_request(
            user=user,
            document=extraction_run.document,
            title=f"Review extraction: {extraction_run.document.title}",
            description=(
                f"Review structured extraction generated using template "
                f"'{extraction_run.template.name}'."
            ),
            review_type=ApprovalRequest.ReviewType.EXTRACTION_REVIEW,
            priority=ApprovalRequest.Priority.MEDIUM,
            assigned_to=assigned_to,
            submitted_payload=submitted_payload,
            ip_address=ip_address,
            user_agent=user_agent,
        )

        extraction_run.status = ExtractionRun.Status.SUBMITTED_FOR_APPROVAL
        extraction_run.approval_request_uuid = approval.uuid
        extraction_run.save(
            update_fields=[
                "status",
                "approval_request_uuid",
                "updated_at",
            ]
        )

        return approval
