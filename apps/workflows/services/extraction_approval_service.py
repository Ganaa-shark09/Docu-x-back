from apps.approvals.models import ApprovalRequest
from apps.approvals.services import ApprovalWorkflowService
from apps.workflows.models import ExtractionRun


class ExtractionApprovalService:
    @classmethod
    def submit_for_approval(
        cls,
        user,
        extraction_run,
        assigned_to=None,
        ip_address=None,
        user_agent="",
    ):
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
