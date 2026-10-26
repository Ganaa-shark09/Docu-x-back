from django.db.models import Q
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied

from apps.approvals.models import ApprovalRequest
from apps.approvals.permissions import IsApprovalWorkflowUser
from apps.approvals.serializers import (
    ApprovalCommentCreateSerializer,
    ApprovalCommentSerializer,
    ApprovalDecisionSerializer,
    ApprovalRejectSerializer,
    ApprovalRequestCreateSerializer,
    ApprovalRequestDetailSerializer,
    ApprovalRequestListSerializer,
    ApprovalRequestUpdateSerializer,
)
from apps.approvals.services import ApprovalWorkflowService
from apps.documents.services import DocumentPermissionService


def get_client_ip(request):
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


class ApprovalRequestViewSet(viewsets.ModelViewSet):
    permission_classes = [IsApprovalWorkflowUser]
    lookup_field = "uuid"
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = [
        "title",
        "description",
        "document__title",
        "requested_by__email",
        "assigned_to__email",
    ]
    ordering_fields = [
        "created_at",
        "updated_at",
        "due_at",
        "priority",
        "status",
        "review_type",
    ]
    ordering = ["-created_at"]

    def get_queryset(self):
        user = self.request.user

        queryset = (
            ApprovalRequest.objects.filter(
                company=user.company,
                is_deleted=False,
            )
            .select_related(
                "company",
                "document",
                "requested_by",
                "assigned_to",
                "reviewed_by",
            )
            .prefetch_related("comments")
        )
        from apps.workflows.models import WorkflowRun
        from apps.workflows.permissions.workflow_permissions import (
            WorkflowPermissionService,
        )

        visible_runs = WorkflowPermissionService.visible_runs(
            user, WorkflowRun.objects.all()
        ).values("pk")
        queryset = queryset.filter(
            Q(workflow_step_run__isnull=True)
            | Q(workflow_step_run__run_id__in=visible_runs)
        )
        allowed_documents = DocumentPermissionService.get_accessible_document_queryset(
            user
        ).values("pk")
        queryset = queryset.filter(document_id__in=allowed_documents)

        if not DocumentPermissionService.is_company_admin(user):
            queryset = queryset.filter(
                Q(workflow_step_run__run_id__in=visible_runs)
                | Q(requested_by=user)
                | Q(assigned_to=user)
                | Q(reviewed_by=user)
                | Q(document__uploaded_by=user)
            )

        review_type = self.request.query_params.get("review_type")
        status_value = self.request.query_params.get("status")
        priority = self.request.query_params.get("priority")
        document_uuid = self.request.query_params.get("document")
        assigned_to_me = self.request.query_params.get("assigned_to_me")

        if review_type:
            queryset = queryset.filter(review_type=review_type)

        if status_value:
            queryset = queryset.filter(status=status_value)

        if priority:
            queryset = queryset.filter(priority=priority)

        if document_uuid:
            queryset = queryset.filter(document__uuid=document_uuid)

        if assigned_to_me and assigned_to_me.lower() in ["true", "1", "yes"]:
            queryset = queryset.filter(assigned_to=user)

        from apps.documents.services.field_permission_service import (
            FieldPermissionService,
        )

        restricted_ids = FieldPermissionService.denied_document_ids(user, "view")
        queryset = queryset.exclude(
            workflow_step_run__run__document_links__document_id__in=restricted_ids
        )
        return queryset.distinct()

    def get_serializer_class(self):
        if self.action == "create":
            return ApprovalRequestCreateSerializer

        if self.action in ["update", "partial_update"]:
            return ApprovalRequestUpdateSerializer

        if self.action == "list":
            return ApprovalRequestListSerializer

        return ApprovalRequestDetailSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["ip_address"] = get_client_ip(self.request)
        context["user_agent"] = self.request.META.get("HTTP_USER_AGENT", "")
        return context

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        approval = serializer.save()

        response_serializer = ApprovalRequestDetailSerializer(
            approval,
            context=self.get_serializer_context(),
        )
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)

    def perform_destroy(self, instance):
        if getattr(instance, "workflow_step_run", None) is not None:
            raise PermissionDenied("Cancel the workflow through a workflow command.")
        instance.soft_delete()

    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request, uuid=None):
        approval = self.get_object()

        serializer = ApprovalDecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        approval = ApprovalWorkflowService.approve(
            user=request.user,
            approval_request=approval,
            reviewed_payload=serializer.validated_data.get("reviewed_payload"),
            reviewer_notes=serializer.validated_data.get("reviewer_notes", ""),
            ip_address=get_client_ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )

        response_serializer = ApprovalRequestDetailSerializer(
            approval,
            context=self.get_serializer_context(),
        )
        return Response(response_serializer.data)

    @action(detail=True, methods=["post"], url_path="reject")
    def reject(self, request, uuid=None):
        approval = self.get_object()

        serializer = ApprovalRejectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        approval = ApprovalWorkflowService.reject(
            user=request.user,
            approval_request=approval,
            reviewer_notes=serializer.validated_data["reviewer_notes"],
            ip_address=get_client_ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )

        response_serializer = ApprovalRequestDetailSerializer(
            approval,
            context=self.get_serializer_context(),
        )
        return Response(response_serializer.data)

    @action(detail=True, methods=["post"], url_path="request-changes")
    def request_changes(self, request, uuid=None):
        approval = self.get_object()

        serializer = ApprovalDecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        reviewer_notes = serializer.validated_data.get("reviewer_notes", "")
        if not reviewer_notes:
            return Response(
                {
                    "reviewer_notes": "Reviewer notes are required when requesting changes."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        approval = ApprovalWorkflowService.request_changes(
            user=request.user,
            approval_request=approval,
            reviewer_notes=reviewer_notes,
            reviewed_payload=serializer.validated_data.get("reviewed_payload"),
            ip_address=get_client_ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )

        response_serializer = ApprovalRequestDetailSerializer(
            approval,
            context=self.get_serializer_context(),
        )
        return Response(response_serializer.data)

    @action(detail=True, methods=["get", "post"], url_path="comments")
    def comments(self, request, uuid=None):
        approval = self.get_object()

        if request.method == "GET":
            serializer = ApprovalCommentSerializer(
                approval.comments.filter(is_deleted=False),
                many=True,
                context=self.get_serializer_context(),
            )
            return Response(serializer.data)

        serializer = ApprovalCommentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        comment = ApprovalWorkflowService.add_comment(
            user=request.user,
            approval_request=approval,
            comment=serializer.validated_data["comment"],
            comment_type=serializer.validated_data.get("comment_type"),
            field_path=serializer.validated_data.get("field_path", ""),
            old_value=serializer.validated_data.get("old_value", ""),
            new_value=serializer.validated_data.get("new_value", ""),
            ip_address=get_client_ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )

        response_serializer = ApprovalCommentSerializer(
            comment,
            context=self.get_serializer_context(),
        )
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)
