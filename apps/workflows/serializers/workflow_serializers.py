from rest_framework import serializers

from apps.workflows.models import (
    WorkflowDefinition,
    WorkflowVersion,
    WorkflowStep,
    WorkflowStepRun,
    WorkflowRun,
    WorkflowFinding,
    WorkflowException,
    WorkflowComment,
    WorkflowArtifact,
)
from apps.workflows.permissions.workflow_permissions import WorkflowPermissionService


class StrictSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if not isinstance(data, dict) or set(data) - set(self.fields):
            raise serializers.ValidationError(
                {"non_field_errors": ["Unexpected command fields."]}
            )
        return super().to_internal_value(data)


class DefinitionWriteSerializer(StrictSerializer):
    name = serializers.CharField(max_length=150)
    code = serializers.SlugField(max_length=80)
    description = serializers.CharField(
        max_length=4000, required=False, allow_blank=True
    )
    workflow_type = serializers.SlugField(max_length=80, default="generic")
    department = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    is_active = serializers.BooleanField(default=True)


class DefinitionStateSerializer(StrictSerializer):
    is_active = serializers.BooleanField()


class VersionPublishSerializer(StrictSerializer):
    entry_step = serializers.SlugField(max_length=80)
    steps = serializers.ListField(
        child=serializers.DictField(), min_length=1, max_length=50
    )
    transitions = serializers.ListField(child=serializers.DictField(), max_length=150)
    allowed_document_types = serializers.ListField(
        child=serializers.CharField(max_length=80), max_length=30, required=False
    )
    configuration = serializers.DictField(required=False)


class StartWorkflowSerializer(StrictSerializer):
    version = serializers.UUIDField()
    documents = serializers.ListField(
        child=serializers.UUIDField(), min_length=1, max_length=20
    )
    idempotency_key = serializers.CharField(max_length=128)
    metadata = serializers.DictField(required=False)


class WorkflowCommandSerializer(StrictSerializer):
    action = serializers.ChoiceField(
        choices=[
            "cancel",
            "assign",
            "accept",
            "approve",
            "reject",
            "resolve_exception",
            "request_changes",
            "comment",
        ]
    )
    expected_revision = serializers.IntegerField(min_value=1)
    idempotency_key = serializers.CharField(max_length=128)
    assignment = serializers.DictField(required=False)
    comment = serializers.CharField(max_length=4000, required=False)
    exception = serializers.UUIDField(required=False)
    resolution = serializers.CharField(max_length=4000, required=False)
    outcome = serializers.ChoiceField(choices=["exception"], required=False)

    def validate(self, attrs):
        extras = set(attrs) - {"action", "expected_revision", "idempotency_key"}
        supported = {
            "cancel": set(),
            "assign": {"assignment"},
            "accept": {"comment"},
            "approve": {"comment"},
            "reject": {"comment"},
            "resolve_exception": {"exception", "resolution", "outcome"},
            "request_changes": {"comment"},
            "comment": {"comment"},
        }
        if extras - supported[attrs["action"]]:
            raise serializers.ValidationError(
                "Fields do not match the workflow command."
            )
        return attrs


class FindingReviewSerializer(StrictSerializer):
    status = serializers.ChoiceField(choices=["accepted", "dismissed", "resolved"])
    owner = serializers.IntegerField(min_value=1, required=False, allow_null=True)


class WorkflowDefinitionSerializer(serializers.ModelSerializer):
    latest_version = serializers.SerializerMethodField()
    capabilities = serializers.SerializerMethodField()

    class Meta:
        model = WorkflowDefinition
        fields = [
            "uuid",
            "name",
            "code",
            "description",
            "workflow_type",
            "department",
            "is_active",
            "latest_version",
            "capabilities",
        ]
        read_only_fields = fields

    def get_latest_version(self, obj):
        version = obj.versions.first()
        return (
            {"uuid": str(version.uuid), "number": version.number} if version else None
        )

    def get_capabilities(self, obj):
        user = self.context["request"].user
        return {
            "administer": WorkflowPermissionService.department_allowed(
                user, obj.department_id, manage=True
            ),
            "start": obj.is_active,
        }


class WorkflowStepSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkflowStep
        fields = ["uuid", "key", "name", "step_type", "configuration"]
        read_only_fields = fields


class WorkflowVersionSerializer(serializers.ModelSerializer):
    steps = WorkflowStepSerializer(many=True, read_only=True)
    transitions = serializers.SerializerMethodField()

    class Meta:
        model = WorkflowVersion
        fields = [
            "uuid",
            "definition",
            "number",
            "entry_step",
            "allowed_document_types",
            "configuration",
            "steps",
            "transitions",
        ]
        read_only_fields = fields

    def get_transitions(self, obj):
        return [
            {"source": t.source.key, "target": t.target.key, "outcome": t.outcome}
            for t in obj.transitions.select_related("source", "target")
        ]


class WorkflowStepRunSerializer(serializers.ModelSerializer):
    key = serializers.CharField(source="step.key", read_only=True)
    step_type = serializers.CharField(source="step.step_type", read_only=True)
    assignment = serializers.SerializerMethodField()
    approval_uuid = serializers.UUIDField(
        source="approval.uuid", read_only=True, allow_null=True
    )

    class Meta:
        model = WorkflowStepRun
        fields = [
            "uuid",
            "key",
            "step_type",
            "status",
            "started_at",
            "completed_at",
            "attempt",
            "error_type",
            "error_message",
            "assignment",
            "approval_uuid",
        ]
        read_only_fields = fields

    def get_assignment(self, obj):
        a = getattr(obj, "assignment", None)
        return (
            {"user": a.user_id, "department": a.department_id, "role": a.role}
            if a
            else None
        )


class WorkflowRunSerializer(serializers.ModelSerializer):
    version_uuid = serializers.UUIDField(source="version.uuid", read_only=True)
    definition_uuid = serializers.UUIDField(
        source="version.definition.uuid", read_only=True
    )
    current_step = serializers.CharField(source="current_step.key", read_only=True)
    documents = serializers.SerializerMethodField()
    capabilities = serializers.SerializerMethodField()

    class Meta:
        model = WorkflowRun
        fields = [
            "uuid",
            "definition_uuid",
            "version_uuid",
            "department",
            "initiated_by",
            "status",
            "current_step",
            "started_at",
            "completed_at",
            "error_type",
            "error_message",
            "correlation_id",
            "revision",
            "documents",
            "capabilities",
        ]
        read_only_fields = fields

    def get_documents(self, obj):
        return [str(doc.uuid) for doc in obj.documents.all()]

    def get_capabilities(self, obj):
        step = obj.step_runs.filter(step=obj.current_step).first()
        user = self.context["request"].user
        open_run = obj.status not in {
            "completed",
            "approved",
            "rejected",
            "failed",
            "cancelled",
        }
        waiting = step is not None and step.status == "waiting"
        review = (
            waiting
            and obj.status == "review_required"
            and step.step.step_type == "human_review"
        )
        approval = waiting and obj.status == "approval_required"
        states = {
            "view": True,
            "cancel": open_run,
            "assign": waiting and open_run,
            "review": review,
            "approve": approval,
            "reject": review or approval,
            "resolve_exception": waiting and obj.status == "exception",
            "comment": open_run,
            "administer": open_run,
        }
        capabilities = {
            action: bool(
                allowed and WorkflowPermissionService.can(user, obj, action, step)
            )
            for action, allowed in states.items()
        }
        capabilities["request_changes"] = bool(
            (review or approval)
            and step.step.outgoing.filter(outcome="exception").exists()
            and WorkflowPermissionService.can(user, obj, "review", step)
        )
        return capabilities


class WorkflowFindingSerializer(serializers.ModelSerializer):
    step_key = serializers.CharField(source="step_run.step.key", read_only=True)
    step_run_status = serializers.CharField(source="step_run.status", read_only=True)

    class Meta:
        model = WorkflowFinding
        fields = [
            "uuid",
            "step_run",
            "step_key",
            "step_run_status",
            "category",
            "severity",
            "status",
            "title",
            "description",
            "data",
            "confidence",
            "evidence",
            "review_state",
            "owner",
        ]
        read_only_fields = fields


class WorkflowExceptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkflowException
        fields = [
            "uuid",
            "step_run",
            "category",
            "status",
            "safe_message",
            "owner",
            "resolved_by",
            "resolved_at",
            "resolution",
        ]
        read_only_fields = fields


class WorkflowCommentSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkflowComment
        fields = ["uuid", "step_run", "author", "body", "created_at"]
        read_only_fields = fields


class WorkflowArtifactSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkflowArtifact
        fields = ["uuid", "step_run", "kind", "data", "evidence", "created_at"]
        read_only_fields = fields
