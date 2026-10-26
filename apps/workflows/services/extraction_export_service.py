import csv
from io import BytesIO, StringIO

from django.http import HttpResponse, JsonResponse
from openpyxl import Workbook
from openpyxl.styles import Font

from apps.approvals.models import ApprovalRequest
from apps.audit.models import AuditLog
from apps.audit.services import AuditLogService
from apps.workflows.models import ExtractionRun
from apps.documents.services import DocumentPermissionService


class ExtractionExportService:
    SUPPORTED_FORMATS = {"json", "csv", "xlsx"}

    @classmethod
    def export(
        cls,
        user,
        extraction_run,
        export_format,
        ip_address=None,
        user_agent="",
    ):
        from apps.workflows.models import ContractReview
        from rest_framework.exceptions import ValidationError

        if ContractReview.objects.filter(extraction_run=extraction_run).exists():
            raise ValidationError(
                "Use the permission-checked finalized contract export."
            )
        DocumentPermissionService.require_action(
            user, extraction_run.document, "export"
        )
        export_format = (export_format or "json").lower()

        if export_format not in cls.SUPPORTED_FORMATS:
            return JsonResponse(
                {
                    "detail": "Unsupported export format.",
                    "supported_formats": sorted(cls.SUPPORTED_FORMATS),
                },
                status=400,
            )

        approval = cls.get_approved_approval_request(extraction_run)

        if not approval:
            AuditLogService.log(
                company=user.company,
                user=user,
                event_type=AuditLog.EventType.EXPORT_BLOCKED,
                severity=AuditLog.Severity.MEDIUM,
                action="Extraction export blocked because extraction is not approved.",
                resource_type="extraction_run",
                resource_uuid=extraction_run.uuid,
                ip_address=ip_address,
                user_agent=user_agent,
                metadata={
                    "extraction_run_uuid": str(extraction_run.uuid),
                    "document_uuid": str(extraction_run.document.uuid),
                    "requested_format": export_format,
                    "status": extraction_run.status,
                    "approval_request_uuid": (
                        str(extraction_run.approval_request_uuid)
                        if extraction_run.approval_request_uuid
                        else None
                    ),
                },
            )

            return JsonResponse(
                {
                    "detail": "Export is allowed only after extraction approval is completed.",
                    "extraction_status": extraction_run.status,
                    "approval_request_uuid": (
                        str(extraction_run.approval_request_uuid)
                        if extraction_run.approval_request_uuid
                        else None
                    ),
                },
                status=403,
            )

        payload = cls.build_export_payload(
            extraction_run=extraction_run,
            approval=approval,
        )

        AuditLogService.log(
            company=user.company,
            user=user,
            event_type=AuditLog.EventType.EXTRACTION_EXPORTED,
            severity=AuditLog.Severity.INFO,
            action="Approved extraction result exported.",
            resource_type="extraction_run",
            resource_uuid=extraction_run.uuid,
            ip_address=ip_address,
            user_agent=user_agent,
            metadata={
                "extraction_run_uuid": str(extraction_run.uuid),
                "document_uuid": str(extraction_run.document.uuid),
                "approval_request_uuid": str(approval.uuid),
                "export_format": export_format,
            },
        )

        if export_format == "json":
            return cls.export_json(payload)

        if export_format == "csv":
            return cls.export_csv(payload)

        return cls.export_xlsx(payload)

    @staticmethod
    def get_approved_approval_request(extraction_run):
        if extraction_run.status != ExtractionRun.Status.APPROVED:
            return None

        if not extraction_run.approval_request_uuid:
            return None

        return ApprovalRequest.objects.filter(
            uuid=extraction_run.approval_request_uuid,
            company=extraction_run.company,
            status=ApprovalRequest.Status.APPROVED,
            is_deleted=False,
        ).first()

    @staticmethod
    def build_export_payload(extraction_run, approval):
        result_fields = extraction_run.result_fields.filter(is_deleted=False).order_by(
            "field_key"
        )

        fields = []

        approval_payload = approval.reviewed_payload or approval.submitted_payload or {}

        for field in result_fields:
            value = field.final_value()

            if isinstance(approval_payload, dict):
                approved_item = approval_payload.get(field.field_key)

                if isinstance(approved_item, dict):
                    value = approved_item.get("value", value)
                elif approved_item is not None:
                    value = approved_item

            fields.append(
                {
                    "field_key": field.field_key,
                    "label": field.label,
                    "value": value,
                    "extracted_value": field.extracted_value,
                    "corrected_value": field.corrected_value,
                    "confidence_score": float(field.confidence_score),
                    "is_corrected": field.is_corrected,
                    "is_required": field.is_required,
                    "is_missing": field.is_missing,
                    "source_text": field.source_text,
                }
            )

        return {
            "export_type": "approved_extraction_result",
            "extraction_run_uuid": str(extraction_run.uuid),
            "document": {
                "uuid": str(extraction_run.document.uuid),
                "title": extraction_run.document.title,
                "document_type": extraction_run.document.document_type,
                "sensitivity_label": extraction_run.document.sensitivity_label,
            },
            "template": {
                "uuid": str(extraction_run.template.uuid),
                "name": extraction_run.template.name,
                "template_type": extraction_run.template.template_type,
            },
            "approval": {
                "uuid": str(approval.uuid),
                "status": approval.status,
                "reviewed_by": approval.reviewed_by.email
                if approval.reviewed_by
                else None,
                "reviewed_at": approval.reviewed_at.isoformat()
                if approval.reviewed_at
                else None,
                "reviewer_notes": approval.reviewer_notes,
            },
            "overall_confidence": float(extraction_run.overall_confidence),
            "fields": fields,
            "final_payload": {item["field_key"]: item["value"] for item in fields},
        }

    @staticmethod
    def safe_filename(value):
        safe = "".join(
            character if character.isalnum() or character in ["-", "_"] else "_"
            for character in value
        )
        return safe.strip("_")[:80] or "extraction"

    @staticmethod
    def safe_cell(value):
        if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
            return "'" + value
        return value

    @classmethod
    def export_json(cls, payload):
        filename = cls.safe_filename(payload["document"]["title"])

        response = JsonResponse(
            payload,
            json_dumps_params={"indent": 2},
        )
        response["Content-Disposition"] = (
            f'attachment; filename="{filename}_approved_extraction.json"'
        )
        return response

    @classmethod
    def export_csv(cls, payload):
        filename = cls.safe_filename(payload["document"]["title"])

        buffer = StringIO()
        writer = csv.writer(buffer)

        writer.writerow(
            [
                "field_key",
                "label",
                "value",
                "extracted_value",
                "corrected_value",
                "confidence_score",
                "is_corrected",
                "is_required",
                "is_missing",
                "source_text",
            ]
        )

        for field in payload["fields"]:
            writer.writerow(
                [
                    cls.safe_cell(item)
                    for item in [
                        field["field_key"],
                        field["label"],
                        field["value"],
                        field["extracted_value"],
                        field["corrected_value"],
                        field["confidence_score"],
                        field["is_corrected"],
                        field["is_required"],
                        field["is_missing"],
                        field["source_text"],
                    ]
                ]
            )

        response = HttpResponse(buffer.getvalue(), content_type="text/csv")
        response["Content-Disposition"] = (
            f'attachment; filename="{filename}_approved_extraction.csv"'
        )
        return response

    @classmethod
    def export_xlsx(cls, payload):
        filename = cls.safe_filename(payload["document"]["title"])

        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = "Approved Extraction"

        metadata_rows = [
            ["Document", payload["document"]["title"]],
            ["Document UUID", payload["document"]["uuid"]],
            ["Template", payload["template"]["name"]],
            ["Approval UUID", payload["approval"]["uuid"]],
            ["Reviewed By", payload["approval"]["reviewed_by"] or ""],
            ["Reviewed At", payload["approval"]["reviewed_at"] or ""],
            ["Overall Confidence", payload["overall_confidence"]],
            [],
        ]

        for row in metadata_rows:
            worksheet.append(row)

        headers = [
            "field_key",
            "label",
            "value",
            "extracted_value",
            "corrected_value",
            "confidence_score",
            "is_corrected",
            "is_required",
            "is_missing",
            "source_text",
        ]

        worksheet.append(headers)

        header_row_number = worksheet.max_row

        for cell in worksheet[header_row_number]:
            cell.font = Font(bold=True)

        for field in payload["fields"]:
            worksheet.append(
                [
                    field["field_key"],
                    field["label"],
                    cls.safe_cell(field["value"]),
                    cls.safe_cell(field["extracted_value"]),
                    cls.safe_cell(field["corrected_value"]),
                    field["confidence_score"],
                    field["is_corrected"],
                    field["is_required"],
                    field["is_missing"],
                    cls.safe_cell(field["source_text"]),
                ]
            )

        for column_cells in worksheet.columns:
            max_length = 0
            column_letter = column_cells[0].column_letter

            for cell in column_cells:
                cell.value = cls.safe_cell(cell.value)
                value = str(cell.value or "")
                max_length = max(max_length, len(value))

            worksheet.column_dimensions[column_letter].width = min(max_length + 2, 50)

        output = BytesIO()
        workbook.save(output)
        output.seek(0)

        response = HttpResponse(
            output.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = (
            f'attachment; filename="{filename}_approved_extraction.xlsx"'
        )
        return response
