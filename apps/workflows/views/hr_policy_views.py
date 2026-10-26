from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.documents.services import DocumentPermissionService
from apps.workflows.models import HRPolicyCategory, HRPolicyVersion, HRPolicySettings
from apps.workflows.serializers.hr_policy_serializers import (
    PolicyCreateSerializer,
    PolicyDraftSerializer,
    PolicySubmitSerializer,
    PolicyQuestionSerializer,
    PolicyCategorySerializer,
    PolicyRevisionSerializer,
    PolicySettingsSerializer,
)
from apps.workflows.serializers.workflow_serializers import WorkflowRunSerializer
from apps.workflows.services.hr_policy_service import (
    HRPolicyService,
    HRPolicyResolver,
    event,
    effective_status,
)
from apps.workflows.services.hr_policy_workflow_service import HRPolicyWorkflowService
from apps.workflows.services.hr_policy_qa_service import HRPolicyQAService
from apps.workflows.services.workflow_definition_service import (
    WorkflowDefinitionService,
)
from apps.workflows.services.workflow_engine_service import WorkflowConflict
from apps.workflows.views.clause_views import NoStoreViewSet, command


def policy_data(policy):
    return {
        "uuid": str(policy.uuid),
        "title": policy.title,
        "code": policy.code,
        "category_uuid": str(policy.category.uuid),
        "department": policy.department_id,
        "revision": policy.revision,
        "draft": policy.draft,
        "review_run_uuid": str(policy.review_run.uuid)
        if policy.review_run_id
        else None,
    }


def version_data(version, sections=False):
    data = {
        "uuid": str(version.uuid),
        "policy_uuid": str(version.policy.uuid),
        "title": version.title,
        "number": version.number,
        "category": version.category.code,
        "department": version.department_id,
        "document_uuid": str(version.document.uuid),
        "effective_from": version.effective_from,
        "effective_until": version.effective_until,
        "scope": version.scope,
        "priority": version.priority,
        "supersedes": str(version.supersedes.uuid) if version.supersedes_id else None,
        "status": version.publication.status,
        "effective_status": effective_status(version),
        "revision": version.publication.revision,
        "coverage": version.coverage,
    }
    if sections:
        data["sections"] = [
            {
                "uuid": str(s.uuid),
                "heading": s.heading,
                "position": s.position,
                "evidence": s.evidence,
            }
            for s in version.sections.all()
        ]
    return data


class HRPolicyViewSet(NoStoreViewSet):
    def get_queryset(self):
        return (
            HRPolicyService.visible(self.request.user)
            .select_related("category", "review_run")
            .order_by("title", "pk")
        )

    def list(self, request):
        # Draft metadata is administrative; published views are separately authorized.
        qs = self.get_queryset()
        if not DocumentPermissionService.is_company_admin(request.user):
            from apps.workspaces.models import DepartmentMembership

            qs = qs.filter(
                department_id__in=DepartmentMembership.objects.filter(
                    user=request.user,
                    role="department_admin",
                    is_active=True,
                    is_deleted=False,
                ).values("department_id")
            )
        page = self.paginate_queryset(qs)
        rows = []
        for policy in page:
            try:
                if policy.draft.get("document_uuid"):
                    HRPolicyService.source(request.user, policy)
                rows.append(policy_data(policy))
            except PermissionDenied:
                continue
        return self.get_paginated_response(rows)

    def retrieve(self, request, uuid=None):
        policy = self.get_object()
        WorkflowDefinitionService.require_admin(request.user, policy.department_id)
        if policy.draft.get("document_uuid"):
            HRPolicyService.source(request.user, policy)
        return Response(policy_data(policy))

    def create(self, request):
        policy = HRPolicyService.create(
            request.user, **command(PolicyCreateSerializer, request)
        )
        return Response(policy_data(policy), status=201)

    @action(detail=True, methods=["post"], url_path="draft")
    def edit_draft(self, request, uuid=None):
        policy = HRPolicyService.edit(
            request.user, self.get_object(), **command(PolicyDraftSerializer, request)
        )
        return Response(policy_data(policy))

    @action(detail=True, methods=["post"])
    def submit(self, request, uuid=None):
        run = HRPolicyWorkflowService.submit(
            request.user, self.get_object(), **command(PolicySubmitSerializer, request)
        )
        return Response(WorkflowRunSerializer(run, context={"request": request}).data)

    @action(detail=True, methods=["post"], url_path="index")
    def index(self, request, uuid=None):
        policy = self.get_object()
        WorkflowDefinitionService.require_admin(request.user, policy.department_id)
        data = command(PolicyRevisionSerializer, request)
        with transaction.atomic():
            policy = HRPolicyService.locked(policy)
            if data["revision"] != policy.revision:
                raise WorkflowConflict()
            result = HRPolicyService.ensure_indexed(request.user, policy)
            event(request.user, policy, "index_checked")
        return Response(result)

    @action(detail=False, methods=["post"])
    def resolve(self, request):
        data = HRPolicyQAService.validate_context(request.data)
        resolved = HRPolicyResolver.resolve(request.user, **data)
        resolved["versions"] = [version_data(v) for v in resolved["versions"]]
        return Response(resolved)

    @action(detail=False, methods=["post"])
    def ask(self, request):
        result = HRPolicyQAService.chat(
            request.user, **command(PolicyQuestionSerializer, request)
        )
        assistant = result["assistant_message"]
        from apps.ai_engine.services.message_visibility_service import (
            MessageVisibilityService,
        )

        if not MessageVisibilityService.visible(assistant, request.user):
            raise PermissionDenied("Policy access changed during answer generation.")
        return Response(
            {
                "conversation_uuid": str(result["conversation"].uuid),
                "message_uuid": str(assistant.uuid),
                "answer": assistant.content,
                "sources": result["sources"],
                "policy": result["policy"],
                "document_context": result["document_context"],
                "firewall": result["firewall"],
                "model_provider": assistant.model_provider,
                "model_name": assistant.model_name,
            }
        )

    @action(detail=False, methods=["get", "post"], url_path="precedence")
    def precedence(self, request):
        WorkflowDefinitionService.require_admin(request.user)
        with transaction.atomic():
            from apps.tenants.models import Company

            Company.objects.select_for_update().get(pk=request.user.company_id)
            settings, _ = HRPolicySettings.objects.get_or_create(
                company=request.user.company
            )
            if request.method == "POST":
                data = command(PolicySettingsSerializer, request)
                if data["revision"] != settings.revision:
                    raise WorkflowConflict()
                settings.precedence = data["precedence"]
                settings.revision += 1
                settings.save()
                event(request.user, settings, "precedence_changed")
            return Response(
                {"revision": settings.revision, "precedence": settings.precedence}
            )


class HRPolicyVersionViewSet(NoStoreViewSet):
    def get_queryset(self):
        return (
            HRPolicyVersion.objects.filter(
                policy__in=HRPolicyService.visible(self.request.user), is_deleted=False
            )
            .select_related(
                "policy",
                "document__text_extraction",
                "publication",
                "category",
                "supersedes",
            )
            .order_by("-created_at")
        )

    def list(self, request):
        qs = self.get_queryset()
        if request.query_params.get("policy"):
            from uuid import UUID

            try:
                policy_uuid = UUID(request.query_params["policy"])
            except ValueError:
                raise ValidationError("Invalid policy UUID.")
            qs = qs.filter(policy__uuid=policy_uuid)
        rows = []
        page = self.paginate_queryset(qs)
        for version in page:
            try:
                rows.append(
                    version_data(HRPolicyService.authorize(request.user, version))
                )
            except PermissionDenied:
                continue
        return self.get_paginated_response(rows)

    def retrieve(self, request, uuid=None):
        return Response(
            version_data(
                HRPolicyService.authorize(request.user, self.get_object()),
                sections=True,
            )
        )

    @action(detail=True, methods=["post"])
    def retire(self, request, uuid=None):
        version = self.get_object()
        HRPolicyService.retire(
            request.user, version, **command(PolicyRevisionSerializer, request)
        )
        return Response(version_data(HRPolicyService.authorize(request.user, version)))

    @action(detail=True, methods=["post"])
    def compare(self, request, uuid=None):
        from rest_framework import serializers

        class CompareSerializer(serializers.Serializer):
            other_version_uuid = serializers.UUIDField()

        data = command(CompareSerializer, request)
        right = self.get_queryset().filter(uuid=data["other_version_uuid"]).first()
        if not right:
            raise PermissionDenied("Policy version is unavailable.")
        return Response(HRPolicyService.compare(request.user, self.get_object(), right))

    @action(detail=True, methods=["get"])
    def export(self, request, uuid=None):
        version = HRPolicyService.authorize(request.user, self.get_object(), "export")
        event(request.user, version, "exported")
        response = JsonResponse(version_data(version, sections=True))
        response["Content-Disposition"] = 'attachment; filename="hr-policy.json"'
        response["Cache-Control"] = "no-store"
        return response


class HRPolicyCategoryViewSet(NoStoreViewSet):
    def get_queryset(self):
        HRPolicyService.fresh(self.request.user)
        return HRPolicyCategory.objects.filter(
            Q(company=self.request.user.company) | Q(company__isnull=True),
            is_deleted=False,
        ).order_by("kind", "code")

    def list(self, request):
        page = self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response(
            [
                {
                    "uuid": str(c.uuid),
                    "kind": c.kind,
                    "code": c.code,
                    "name": c.name,
                    "is_active": c.is_active,
                    "scope": "system" if c.company_id is None else "company",
                }
                for c in page
            ]
        )

    def create(self, request):
        WorkflowDefinitionService.require_admin(request.user)
        data = command(PolicyCategorySerializer, request)
        if self.get_queryset().filter(kind=data["kind"], code=data["code"]).exists():
            raise ValidationError("Policy taxonomy code already exists.")
        with transaction.atomic():
            category = HRPolicyCategory.objects.create(
                company=request.user.company, **data
            )
            event(request.user, category, "category_created")
        return Response({"uuid": str(category.uuid), **data}, status=201)

    @action(detail=True, methods=["post"])
    def deactivate(self, request, uuid=None):
        WorkflowDefinitionService.require_admin(request.user)
        category = self.get_object()
        if category.company_id is None:
            raise PermissionDenied("System policy categories are protected.")
        category.is_active = False
        category.save(update_fields=["is_active", "updated_at"])
        event(request.user, category, "category_deactivated")
        return Response({"uuid": str(category.uuid), "is_active": False})
