import json
import re

from django.db import transaction
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.accounts.models import User
from apps.workspaces.models import Department
from apps.workflows.models import (
    WorkflowDefinition,
    WorkflowStep,
    WorkflowTransition,
    WorkflowVersion,
)
from apps.workflows.permissions.workflow_permissions import WorkflowPermissionService


def bounded_json(value, limit=32768):
    try:
        serialized = json.dumps(value, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise ValidationError("Invalid JSON value.")
    if len(serialized.encode()) > limit:
        raise ValidationError("Structured data exceeds the supported size.")
    return value


class WorkflowDefinitionService:
    @classmethod
    def require_admin(cls, user, department_id=None):
        if not WorkflowPermissionService.active(
            user
        ) or not WorkflowPermissionService.department_allowed(
            user, department_id, manage=True
        ):
            raise PermissionDenied("Workflow administration permission is required.")
        if (
            department_id
            and not Department.objects.filter(
                pk=department_id,
                company_id=user.company_id,
                is_active=True,
                is_deleted=False,
            ).exists()
        ):
            raise ValidationError("Department must be active and company-scoped.")

    @classmethod
    @transaction.atomic
    def create(cls, user, **attrs):
        cls.require_admin(user, attrs.get("department_id"))
        definition = WorkflowDefinition(company_id=user.company_id, **attrs)
        definition.full_clean()
        definition.save()
        return definition

    @classmethod
    def validate_assignment(cls, company_id, config):
        if not isinstance(config, dict) or set(config) - {"user", "department", "role"}:
            raise ValidationError("Assignment accepts only user, department and role.")
        if not config or not any(config.values()):
            raise ValidationError("An assignment principal is required.")
        if config.get("user") and (config.get("role") or config.get("department")):
            raise ValidationError("Assign a user or a role/department group.")
        if config.get("user") is not None:
            if (
                type(config["user"]) is not int
                or not User.objects.filter(
                    pk=config["user"], company_id=company_id, is_active=True
                ).exists()
            ):
                raise ValidationError(
                    "Assigned user must be active and company-scoped."
                )
        if config.get("department") is not None:
            if (
                type(config["department"]) is not int
                or not Department.objects.filter(
                    pk=config["department"],
                    company_id=company_id,
                    is_active=True,
                    is_deleted=False,
                ).exists()
            ):
                raise ValidationError(
                    "Assigned department must be active and company-scoped."
                )
        if config.get("role") and config["role"] not in User.Role.values:
            raise ValidationError("Unsupported assignment role.")

    @classmethod
    def validate_config(cls, company_id, kind, config):
        if not isinstance(config, dict):
            raise ValidationError("Step configuration must be an object.")
        bounded_json(config)
        allowed = {
            "ai": {"instruction", "top_k"},
            "extraction": {"template"},
            "rule": {"source_step", "path", "operator", "value"},
            "validation": {"source_step", "path", "operator", "value"},
            "human_review": {"assignment"},
            "approval": {"assignment"},
            "assignment": {"assignment"},
            "integration": {"adapter", "options"},
            "processing": {"adapter", "operation", "options", "exception_assignment"},
            "notification": set(),
            "terminal": {"status"},
        }
        if (
            not isinstance(kind, str)
            or kind not in allowed
            or set(config) - allowed[kind]
        ):
            raise ValidationError("Unsupported step type or configuration key.")
        if kind == "processing":
            from apps.workflows.services.workflow_processing_service import (
                WorkflowProcessingService,
            )

            WorkflowProcessingService.validate(company_id, config)
            if "exception_assignment" in config:
                cls.validate_assignment(company_id, config["exception_assignment"])
        elif kind in {"human_review", "approval", "assignment"}:
            cls.validate_assignment(company_id, config.get("assignment", {}))
        elif kind == "ai":
            if (
                not isinstance(config.get("instruction"), str)
                or not 1 <= len(config["instruction"]) <= 4000
            ):
                raise ValidationError("AI instruction must contain 1–4000 characters.")
            if (
                type(config.get("top_k", 10)) is not int
                or not 1 <= config.get("top_k", 10) <= 20
            ):
                raise ValidationError("AI top_k must be between 1 and 20.")
        elif kind == "extraction":
            from apps.workflows.models import ExtractionTemplate

            try:
                valid = (
                    ExtractionTemplate.objects.filter(
                        uuid=config.get("template"), is_deleted=False, is_active=True
                    )
                    .filter(company_id=company_id)
                    .exists()
                )
            except (ValueError, TypeError):
                valid = False
            if not valid:
                raise ValidationError(
                    "Extraction template must be active and company-scoped."
                )
        elif kind in {"rule", "validation"}:
            if (
                not isinstance(config.get("operator"), str)
                or config.get("operator") not in {"exists", "eq", "ne", "gte", "lte"}
                or not isinstance(config.get("source_step"), str)
                or not isinstance(config.get("path"), str)
                or not re.fullmatch(
                    r"[a-zA-Z0-9_]+(?:\.[a-zA-Z0-9_]+)*", config["path"]
                )
            ):
                raise ValidationError("Invalid rule source, path or operator.")
            if config["operator"] in {"gte", "lte"} and type(
                config.get("value")
            ) not in {int, float}:
                raise ValidationError("Numeric comparison requires a numeric value.")
        elif kind == "terminal" and (
            not isinstance(config.get("status", "completed"), str)
            or config.get("status", "completed")
            not in {"completed", "rejected", "approved"}
        ):
            raise ValidationError("Invalid terminal state.")
        elif kind == "integration":
            from apps.workflows.services.workflow_executor_service import (
                WorkflowExecutorService,
            )

            if (
                not isinstance(config.get("adapter"), str)
                or config.get("adapter")
                not in WorkflowExecutorService.integration_adapters
                or not isinstance(config.get("options", {}), dict)
            ):
                raise ValidationError(
                    "Integration adapter must be registered by the server."
                )

    @classmethod
    @transaction.atomic
    def publish(
        cls,
        user,
        definition,
        *,
        entry_step,
        steps,
        transitions,
        allowed_document_types=None,
        configuration=None,
    ):
        definition = WorkflowDefinition.objects.select_for_update().get(
            pk=definition.pk, company_id=user.company_id, is_deleted=False
        )
        cls.require_admin(user, definition.department_id)
        if (
            not isinstance(steps, list)
            or not 1 <= len(steps) <= 50
            or not isinstance(transitions, list)
            or len(transitions) > 150
        ):
            raise ValidationError(
                "A workflow requires 1–50 steps and at most 150 transitions."
            )
        configuration = {} if configuration is None else configuration
        if not isinstance(configuration, dict) or set(configuration) - {"max_attempts"}:
            raise ValidationError("Unsupported version configuration.")
        if (
            type(configuration.get("max_attempts", 4)) is not int
            or not 1 <= configuration.get("max_attempts", 4) <= 4
        ):
            raise ValidationError("Workflow max_attempts must be between 1 and 4.")
        allowed_document_types = allowed_document_types or []
        if (
            not isinstance(allowed_document_types, list)
            or len(allowed_document_types) > 30
            or any(
                not isinstance(x, str) or not 1 <= len(x) <= 80
                for x in allowed_document_types
            )
        ):
            raise ValidationError("Invalid allowed document types.")
        keys = {}
        for step in steps:
            if not isinstance(step, dict) or set(step) - {
                "key",
                "name",
                "step_type",
                "configuration",
            }:
                raise ValidationError("Invalid step fields.")
            key = step.get("key", "")
            if (
                not isinstance(key, str)
                or not re.fullmatch(r"[-a-zA-Z0-9_]{1,80}", key)
                or key in keys
                or not isinstance(step.get("name"), str)
                or not 1 <= len(step["name"]) <= 150
            ):
                raise ValidationError(
                    "Step keys must be unique and names are required."
                )
            cls.validate_config(
                definition.company_id,
                step.get("step_type"),
                step.get("configuration", {}),
            )
            keys[key] = step
        if entry_step not in keys:
            raise ValidationError("Entry step does not exist.")
        outcomes = {
            "human_review": {"accepted", "rejected", "exception"},
            "approval": {"approved", "rejected", "exception"},
            "extraction": {"success", "exception"},
            "processing": {"success", "exception"},
            "rule": {"pass", "fail"},
            "validation": {"pass", "fail"},
            "terminal": set(),
        }
        edges = {}
        for edge in transitions:
            if (
                not isinstance(edge, dict)
                or set(edge) != {"source", "target", "outcome"}
                or any(not isinstance(v, str) for v in edge.values())
                or edge["source"] not in keys
                or edge["target"] not in keys
            ):
                raise ValidationError(
                    "Transition must reference steps in this version."
                )
            possible = outcomes.get(keys[edge["source"]]["step_type"], {"success"})
            pair = (edge["source"], edge["outcome"])
            if edge["outcome"] not in possible or pair in edges:
                raise ValidationError(
                    "Transition outcomes must be supported and deterministic."
                )
            edges[pair] = edge["target"]
        for key, step in keys.items():
            required = outcomes.get(step["step_type"], {"success"}) - {"exception"}
            if any((key, outcome) not in edges for outcome in required):
                raise ValidationError(
                    "Every normal outcome requires an explicit transition."
                )
            source = step.get("configuration", {}).get("source_step")
            if source and source not in keys:
                raise ValidationError("Rule source step does not exist.")
        visited, active = set(), set()

        def visit(key):
            if key in active:
                raise ValidationError("Cyclic workflow graphs are not supported.")
            if key in visited:
                return
            active.add(key)
            for (source, _), target in edges.items():
                if source == key:
                    visit(target)
            active.remove(key)
            visited.add(key)

        visit(entry_step)
        if visited != set(keys):
            raise ValidationError("All workflow steps must be reachable.")
        predecessors = {
            key: {source for (source, _), target in edges.items() if target == key}
            for key in keys
        }
        dominators = {}

        def ancestors(key):
            if key not in dominators:
                parents = predecessors[key]
                common = (
                    set.intersection(*(ancestors(p) for p in parents))
                    if parents
                    else set()
                )
                dominators[key] = common | {key}
            return dominators[key]

        for key, step in keys.items():
            source = step.get("configuration", {}).get("source_step")
            if source and (
                source == key
                or source not in ancestors(key)
                or keys[source]["step_type"]
                not in {"ai", "extraction", "integration", "processing"}
            ):
                raise ValidationError(
                    "Rule input must be produced on every path before the rule."
                )
        latest = definition.versions.order_by("-number").first()
        version = WorkflowVersion.objects.create(
            definition=definition,
            number=(latest.number + 1 if latest else 1),
            entry_step=entry_step,
            allowed_document_types=allowed_document_types,
            configuration=configuration,
            published_by=user,
        )
        for value in keys.values():
            if value["step_type"] == "processing":
                from apps.workflows.services.workflow_processing_service import (
                    WorkflowProcessingService,
                )

                adapter = WorkflowProcessingService.adapter(
                    value["configuration"]["adapter"]
                )
                value["execution_snapshot"] = adapter.snapshot(
                    value["configuration"]["operation"],
                    value["configuration"].get("options", {}),
                )
                bounded_json(value["execution_snapshot"])
            if value["step_type"] == "extraction":
                from apps.workflows.models import ExtractionTemplate

                template = ExtractionTemplate.objects.get(
                    uuid=value["configuration"]["template"]
                )
                fields = list(
                    template.fields.filter(is_deleted=False)
                    .order_by("order")
                    .values(
                        "field_key",
                        "label",
                        "regex_pattern",
                        "is_required",
                        "extraction_hint",
                        "default_value",
                    )
                )
                if not 1 <= len(fields) <= 100:
                    raise ValidationError(
                        "Extraction adapter requires 1–100 configured fields."
                    )
                bounded_json(fields)
                for field in fields:
                    try:
                        re.compile(field["regex_pattern"])
                    except re.error:
                        raise ValidationError("Extraction field pattern is invalid.")
                value["execution_snapshot"] = {"fields": fields}
        stored = {
            key: WorkflowStep.objects.create(version=version, **value)
            for key, value in keys.items()
        }
        for (source, outcome), target in edges.items():
            WorkflowTransition.objects.create(
                version=version,
                source=stored[source],
                target=stored[target],
                outcome=outcome,
            )
        from apps.workflows.services.workflow_processing_service import (
            WorkflowProcessingService,
        )

        for adapter, options in WorkflowProcessingService.processors(version):
            if hasattr(adapter, "validate_version"):
                adapter.validate_version(version, options)
        from apps.workflows.services.workflow_events import workflow_event

        workflow_event(
            None,
            user,
            "definition_published",
            definition=definition,
            version=version.number,
        )
        return version
