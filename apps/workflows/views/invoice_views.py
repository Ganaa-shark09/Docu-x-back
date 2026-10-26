from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q
from django.utils import timezone
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404, JsonResponse
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from apps.documents.services.invoice_readiness_service import InvoiceReadinessService
from apps.documents.services import DocumentPermissionService
from apps.documents.services.field_permission_service import FieldPermissionService
from apps.workflows.models import (
    InvoiceBatch,
    InvoiceBatchItem,
    InvoiceOutputVersion,
    InvoiceRecord,
    WorkflowRun,
    WorkflowRunDocument,
)
from apps.workflows.permissions.workflow_permissions import WorkflowPermissionService
from apps.workflows.serializers.invoice_serializers import (
    InvoiceCorrectionSerializer,
    InvoiceDefinitionSerializer,
    InvoiceOutputSerializer,
    InvoiceSerializer,
    InvoiceRegisterSerializer,
    InvoiceBatchCreateSerializer,
    InvoiceBatchSerializer,
    InvoiceAccountingPostSerializer,
)
from apps.workflows.serializers.workflow_serializers import (
    WorkflowDefinitionSerializer,
    WorkflowVersionSerializer,
)
from apps.workflows.services.invoice_definition_service import InvoiceDefinitionService
from apps.workflows.services.invoice_batch_service import InvoiceBatchService
from apps.integrations.services.invoice_accounting import InvoiceAccountingService
from apps.workflows.services.workflow_engine_service import (
    WorkflowConflict,
    WorkflowEngineService,
)
from apps.workflows.services.workflow_events import workflow_event


class InvoiceBatchPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


class InvoiceBatchViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    permission_classes = [IsAuthenticated]
    serializer_class = InvoiceBatchSerializer
    lookup_field = "uuid"
    pagination_class = InvoiceBatchPagination

    def get_queryset(self):
        user = self.request.user
        if not WorkflowPermissionService.active(user):
            return InvoiceBatch.objects.none()
        accessible = (
            DocumentPermissionService.get_accessible_document_queryset(
                user, required_access="view", scope="internal"
            )
            .filter(
                Q(retention_expires_at__isnull=True)
                | Q(retention_expires_at__gt=timezone.now())
            )
            .values("pk")
        )
        denied = InvoiceBatchItem.objects.filter(batch_id=OuterRef("pk")).exclude(
            document_id__in=accessible
        )
        queryset = (
            InvoiceBatch.objects.filter(
                company_id=user.company_id,
                is_deleted=False,
                version__definition_id__in=WorkflowPermissionService.definitions(
                    user
                ).values("pk"),
            )
            .annotate(_denied=Exists(denied))
            .filter(_denied=False)
            .select_related("version")
            .prefetch_related("items__document", "items__run")
        )
        search = self.request.query_params.get("search", "").strip()
        return queryset.filter(name__icontains=search[:100]) if search else queryset

    def create(self, request):
        serializer = InvoiceBatchCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        batch, created = InvoiceBatchService.create(
            request.user,
            name=data["name"],
            version_uuid=data["version"],
            document_uuids=[str(value) for value in data["documents"]],
            idempotency_key=data["idempotency_key"],
        )
        batch = self.get_queryset().get(pk=batch.pk)
        return Response(self.get_serializer(batch).data, status=201 if created else 200)

    @action(detail=True, methods=["post"], url_path="retry-item")
    @transaction.atomic
    def retry_item(self, request, uuid=None):
        batch = self.get_object()
        item_uuid = request.data.get("item_uuid")
        if not item_uuid or set(request.data) != {"item_uuid"}:
            raise ValidationError("Select one failed batch item.")
        item = (
            InvoiceBatchItem.objects.select_for_update()
            .filter(batch=batch, uuid=item_uuid, is_deleted=False)
            .first()
        )
        if not item:
            raise Http404
        if item.run_id or not item.error_code:
            raise WorkflowConflict(
                "Only an item whose run did not start can be retried here."
            )
        InvoiceReadinessService.require_batch_source(request.user, item.document)
        if item.document.status in {"failed", "archived"}:
            raise ValidationError(
                "Retry document processing or replace the source before starting this item."
            )
        if item.document.status != "ready":
            item.error_code = "awaiting_document"
            item.error_message = ""
            item.save(update_fields=["error_code", "error_message", "updated_at"])
            InvoiceBatchService.enqueue(batch)
            return Response(
                self.get_serializer(self.get_queryset().get(pk=batch.pk)).data
            )
        run, _ = WorkflowEngineService.start(
            request.user,
            version_uuid=batch.version.uuid,
            document_uuids=[str(item.document.uuid)],
            idempotency_key=f"invoice-batch:{batch.uuid}:{item.position}",
            metadata={"invoice_batch_uuid": str(batch.uuid)},
        )
        item.run = run
        item.error_code = item.error_message = ""
        item.save(update_fields=["run", "error_code", "error_message", "updated_at"])
        return Response(self.get_serializer(self.get_queryset().get(pk=batch.pk)).data)


class InvoiceViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    permission_classes = [IsAuthenticated]
    serializer_class = InvoiceSerializer
    lookup_field = "uuid"
    pagination_class = InvoiceBatchPagination

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        return response

    def get_queryset(self):
        return InvoiceRecord.objects.filter(
            is_deleted=False,
            company_id=self.request.user.company_id,
            workflow_run__version__definition__workflow_type="invoice",
            workflow_run__in=WorkflowPermissionService.visible_runs(
                self.request.user, WorkflowRun.objects.all()
            ),
        ).select_related("document", "workflow_run", "extraction_run")

    def get_serializer_class(self):
        return InvoiceRegisterSerializer if self.action == "list" else InvoiceSerializer

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        if self.action not in {"list", "summary"}:
            return queryset

        # A workflow can use several documents. All must permit field-level
        # viewing before a record or aggregate count can be exposed.
        user = self.request.user
        denied_ids = FieldPermissionService.denied_document_ids(user, "view")
        denied_links = WorkflowRunDocument.objects.filter(
            run_id=OuterRef("workflow_run_id"),
            document_id__in=denied_ids,
        )
        queryset = (
            queryset.exclude(document_id__in=denied_ids)
            .annotate(_restricted_source=Exists(denied_links))
            .filter(_restricted_source=False)
        )

        status = self.request.query_params.get("status", "").strip()
        if status:
            if status not in WorkflowRun.Status.values:
                raise ValidationError({"status": "Unknown invoice workflow status."})
            queryset = queryset.filter(workflow_run__status=status)

        search = self.request.query_params.get("search", "").strip()
        if len(search) > 100:
            raise ValidationError({"search": "Search must be 100 characters or fewer."})
        if search:
            queryset = queryset.filter(
                Q(invoice_number__icontains=search)
                | Q(document__title__icontains=search)
                | Q(normalized__vendor_name__icontains=search)
            )
        if self.action == "list":
            available_outputs = InvoiceOutputVersion.objects.filter(
                invoice_id=OuterRef("pk"),
                is_deleted=False,
            )
            queryset = queryset.annotate(has_final_output=Exists(available_outputs))
        return queryset.order_by("-created_at", "-pk")

    @action(detail=False, methods=["get"], url_path="summary")
    def summary(self, request):
        """KPI counts respect the same filters and permissions as the register."""
        invoices = self.filter_queryset(self.get_queryset())
        counts = {status: 0 for status in WorkflowRun.Status.values}
        for row in invoices.values("workflow_run__status").annotate(total=Count("pk")):
            counts[row["workflow_run__status"]] = row["total"]
        return Response({"total": sum(counts.values()), "by_status": counts})

    def get_object(self):
        obj = super().get_object()
        if not WorkflowPermissionService.content_allowed(
            self.request.user, obj.workflow_run
        ):
            raise PermissionDenied(
                "Current invoice field permissions deny content access."
            )
        return obj

    @action(detail=True, methods=["get"], url_path="accounting-status")
    def accounting_status(self, request, uuid=None):
        return Response(
            InvoiceAccountingService.status(request.user, self.get_object())
        )

    @action(detail=True, methods=["post"], url_path="post-accounting")
    def post_accounting(self, request, uuid=None):
        serializer = InvoiceAccountingPostSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        handoff, created = InvoiceAccountingService.request_post(
            request.user,
            self.get_object(),
            serializer.validated_data["idempotency_key"],
        )
        return Response(
            {"handoff_uuid": str(handoff.uuid), "status": handoff.status},
            status=202 if created else 200,
        )

    @action(detail=True, methods=["post"], url_path="reconcile-accounting")
    def reconcile_accounting(self, request, uuid=None):
        if request.data:
            raise ValidationError(
                "Reconciliation does not accept invoice content or arbitrary fields."
            )
        handoff = InvoiceAccountingService.reconcile(request.user, self.get_object())
        return Response({"handoff_uuid": str(handoff.uuid), "status": handoff.status})

    @action(detail=False, methods=["post"], url_path="definitions")
    @transaction.atomic
    def definitions(self, request):
        serializer = InvoiceDefinitionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            definition, version = InvoiceDefinitionService.create(
                request.user, **serializer.validated_data
            )
        except DjangoValidationError:
            raise ValidationError(
                "Invoice definition conflicts with an existing definition or ownership constraint."
            )
        return Response(
            {
                "definition": WorkflowDefinitionSerializer(
                    definition, context=self.get_serializer_context()
                ).data,
                "version": WorkflowVersionSerializer(
                    version, context=self.get_serializer_context()
                ).data,
            },
            status=201,
        )

    @action(detail=True, methods=["get"])
    def fields(self, request, uuid=None):
        invoice = self.get_object()
        fields = (
            invoice.extraction_run.result_fields.prefetch_related("corrections").all()
            if invoice.extraction_run_id
            else []
        )
        provenance = self.get_serializer(invoice).data["field_provenance"]
        return Response(
            [
                {
                    **provenance[f.field_key],
                    "field_key": f.field_key,
                    "corrections": InvoiceCorrectionSerializer(
                        f.corrections.all(), many=True
                    ).data,
                }
                for f in fields
            ]
        )

    @action(detail=True, methods=["get"])
    def outputs(self, request, uuid=None):
        return Response(
            InvoiceOutputSerializer(
                self.get_object().outputs.filter(is_deleted=False), many=True
            ).data
        )

    @action(detail=True, methods=["get"])
    def export(self, request, uuid=None):
        invoice = self.get_object()
        DocumentPermissionService.require_action(
            request.user, invoice.document, "export"
        )
        if invoice.workflow_run.status not in {"completed", "approved"}:
            raise ValidationError(
                "Invoice export requires a successfully completed workflow."
            )
        output = invoice.outputs.filter(is_deleted=False).first()
        if not output:
            raise Http404
        workflow_event(
            invoice.workflow_run,
            request.user,
            "invoice_export_prepared",
            invoice_revision=output.revision,
            digest=output.digest,
        )
        response = JsonResponse(output.data)
        response["Content-Disposition"] = (
            f'attachment; filename="invoice-{invoice.uuid}-r{output.revision}.json"'
        )
        response["Cache-Control"] = "no-store"
        return response
