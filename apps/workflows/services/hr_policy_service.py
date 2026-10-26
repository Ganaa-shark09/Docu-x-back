import hashlib
import re
from datetime import date
from uuid import UUID

from django.db import transaction
from django.db.models import Q, Max
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.audit.services import AuditLogService
from apps.documents.models import Document
from apps.documents.services import DocumentPermissionService
from apps.documents.services.field_permission_service import FieldPermissionService
from apps.workflows.models import (
    HRPolicy,
    HRPolicyCategory,
    HRPolicyVersion,
    HRPolicyPublication,
    HRPolicySection,
    HRPolicySettings,
)
from apps.workflows.permissions.workflow_permissions import WorkflowPermissionService
from apps.workflows.services.clause_detection_service import ClauseDetectionService
from apps.workflows.services.contract_extraction_service import evidence
from apps.workflows.services.workflow_definition_service import (
    WorkflowDefinitionService,
    bounded_json,
)
from apps.workflows.services.workflow_engine_service import (
    WorkflowConflict,
    WorkflowEngineService,
)


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def event(user, resource, action):
    AuditLogService.log(
        company=user.company,
        user=user,
        event_type="workflow_event",
        action="HR policy " + action,
        resource_type="hr_policy",
        resource_uuid=resource.uuid,
        metadata={"event": action},
    )


def categories(user, kind="policy"):
    return HRPolicyCategory.objects.filter(
        Q(company_id=user.company_id) | Q(company__isnull=True),
        kind=kind,
        is_active=True,
        is_deleted=False,
    )


def effective_status(version, day=None):
    day = day or timezone.localdate()
    if version.publication.status == "retired":
        return "retired"
    if version.effective_from > day:
        return "scheduled"
    if version.effective_until and version.effective_until <= day:
        return "expired"
    if version.replacements.filter(effective_from__lte=day).exists():
        return "superseded"
    return "effective"


def validate_draft(user, value, complete=False):
    allowed = {
        "document_uuid",
        "effective_from",
        "effective_until",
        "employee_categories",
        "locations",
        "priority",
        "supersedes",
    }
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValidationError("Invalid policy draft fields.")
    bounded_json(value, 8192)
    result = dict(value)
    for key in ("document_uuid", "supersedes"):
        if key in result and result[key] is not None:
            try:
                result[key] = str(UUID(str(result[key])))
            except (ValueError, TypeError, AttributeError):
                raise ValidationError("Invalid policy source or version UUID.")
    for key in ("effective_from", "effective_until"):
        if result.get(key):
            try:
                result[key] = date.fromisoformat(result[key]).isoformat()
            except (ValueError, TypeError):
                raise ValidationError("Policy dates must be explicit ISO dates.")
    if complete and not result.get("effective_from"):
        raise ValidationError("An effective date must be reviewed explicitly.")
    if result.get("effective_until") and (
        not result.get("effective_from")
        or result["effective_until"] <= result["effective_from"]
    ):
        raise ValidationError("The exclusive policy end date must follow its start.")
    priority = result.get("priority", 0)
    if type(priority) is not int or not -100 <= priority <= 100:
        raise ValidationError("Policy priority must be an integer from -100 to 100.")
    result["priority"] = priority
    for key, kind in (("employee_categories", "employee"), ("locations", "location")):
        values = result.get(key, [])
        if (
            not isinstance(values, list)
            or len(values) > 30
            or any(not isinstance(v, str) for v in values)
            or len(set(values)) != len(values)
        ):
            raise ValidationError(
                "Policy scope must contain bounded distinct configured codes."
            )
        if set(values) - set(categories(user, kind).values_list("code", flat=True)):
            raise ValidationError("Policy scope contains unconfigured values.")
        result[key] = sorted(values)
    return result


class HRPolicyService:
    @staticmethod
    def locked(policy):
        # Match the generic executor's run -> company -> domain lock order.
        from apps.tenants.models import Company

        snapshot = HRPolicy.objects.get(pk=policy.pk)
        if snapshot.review_run_id:
            WorkflowEngineService.locked(snapshot.review_run)
        Company.objects.select_for_update().get(pk=snapshot.company_id)
        current = HRPolicy.objects.select_for_update().get(pk=snapshot.pk)
        if current.review_run_id != snapshot.review_run_id:
            raise WorkflowConflict("Policy review changed; refresh the policy.")
        return current

    @staticmethod
    def fresh(user):
        user.refresh_from_db()
        if not WorkflowPermissionService.active(user):
            raise PermissionDenied("An active company account is required.")

    @classmethod
    def visible(cls, user):
        cls.fresh(user)
        qs = HRPolicy.objects.filter(company_id=user.company_id, is_deleted=False)
        if not DocumentPermissionService.is_company_admin(user):
            qs = qs.filter(
                Q(department__isnull=True)
                | Q(
                    department_id__in=DocumentPermissionService.get_user_department_ids(
                        user
                    )
                )
            )
        return qs.filter(
            Q(department__isnull=True)
            | Q(department__is_active=True, department__is_deleted=False)
        )

    @classmethod
    def authorize(cls, user, version, action="view"):
        cls.fresh(user)
        # Never trust cached document permissions or source text.
        version = HRPolicyVersion.objects.select_related(
            "policy", "category", "publication", "document__text_extraction"
        ).get(pk=version.pk)
        doc = version.document
        if not cls.visible(user).filter(
            pk=version.policy_id
        ).exists() or not WorkflowPermissionService.department_allowed(
            user, version.department_id
        ):
            raise PermissionDenied("Policy version is unavailable.")
        if (
            version.is_deleted
            or version.category.is_deleted
            or not version.category.is_active
            or doc.scope != "internal"
            or doc.is_deleted
        ):
            raise PermissionDenied("Policy source is unavailable.")
        for required in {"view", action}:
            DocumentPermissionService.require_action(user, doc, required)
            if FieldPermissionService.has_restrictions(user, doc, required):
                raise PermissionDenied("Field policies deny this policy derivative.")
        if doc.retention_expires_at and doc.retention_expires_at <= timezone.now():
            raise PermissionDenied("Policy source retention has expired.")
        text, coverage = ClauseDetectionService.source(doc)
        if (
            not coverage["complete"]
            or digest(text) != version.source_digest
            or doc.governance_revision != version.governance_revision
        ):
            raise PermissionDenied("Pinned policy source is no longer available.")
        return version

    @classmethod
    @transaction.atomic
    def create(cls, user, *, title, code, category_uuid, department=None, draft=None):
        cls.fresh(user)
        WorkflowDefinitionService.require_admin(user, department)
        from apps.tenants.models import Company

        Company.objects.select_for_update().get(pk=user.company_id)
        if HRPolicy.objects.filter(company=user.company, code=code).exists():
            raise ValidationError("Policy code already exists.")
        category = categories(user).filter(uuid=category_uuid).first()
        if not category:
            raise ValidationError("Policy category is unavailable.")
        policy = HRPolicy.objects.create(
            company=user.company,
            department_id=department,
            title=title,
            code=code,
            category=category,
            draft=validate_draft(user, draft or {}),
        )
        if policy.draft.get("document_uuid"):
            cls.source(user, policy)
        event(user, policy, "created")
        return policy

    @classmethod
    @transaction.atomic
    def edit(cls, user, policy, revision, draft, workflow_revision=None):
        cls.fresh(user)
        policy = cls.locked(policy)
        if not cls.visible(user).filter(pk=policy.pk).exists():
            raise PermissionDenied("Policy is unavailable.")
        if policy.revision != revision:
            raise WorkflowConflict()
        run = None
        if policy.review_run_id:
            run = WorkflowEngineService.locked(policy.review_run)
            if run.status not in WorkflowEngineService.CLOSED:
                if (
                    run.current_step.key not in {"review_policy", "ingest_policy"}
                    or run.status not in {"review_required", "exception"}
                    or workflow_revision != run.revision
                ):
                    raise WorkflowConflict(
                        "Policy changes require a current review boundary."
                    )
                WorkflowPermissionService.require(
                    user, run, "review", run.step_runs.get(step=run.current_step)
                )
        if not run or run.status in WorkflowEngineService.CLOSED:
            WorkflowDefinitionService.require_admin(user, policy.department_id)
        if policy.draft.get("document_uuid"):
            cls.source(user, policy)
        policy.draft = validate_draft(user, draft)
        policy.revision += 1
        policy.save(update_fields=["draft", "revision", "updated_at"])
        if run and run.status not in WorkflowEngineService.CLOSED:
            run.metadata["policy_revision"] = policy.revision
            run.revision += 1
            run.save(update_fields=["metadata", "revision", "updated_at"])
        event(user, policy, "draft_changed")
        return policy

    @classmethod
    def source(cls, user, policy, complete=False):
        cls.fresh(user)
        if (
            not cls.visible(user).filter(pk=policy.pk).exists()
            or not categories(user).filter(pk=policy.category_id).exists()
        ):
            raise PermissionDenied("Policy scope or category is unavailable.")
        draft = validate_draft(user, policy.draft, complete=complete)
        doc = (
            Document.objects.select_related("text_extraction")
            .filter(
                uuid=draft.get("document_uuid"),
                company_id=user.company_id,
                scope="internal",
                is_deleted=False,
            )
            .first()
        )
        if not doc:
            raise PermissionDenied("Policy source is unavailable.")
        for action in ("view", "extract", "ai"):
            DocumentPermissionService.require_action(user, doc, action)
            if action in {
                "view",
                "ai",
                "export",
            } and FieldPermissionService.has_restrictions(user, doc, action):
                raise PermissionDenied("Field policies deny policy processing.")
        text, coverage = ClauseDetectionService.source(doc)
        if complete and (
            not coverage["complete"]
            or not doc.is_ai_ready
            or not doc.is_indexed
            or doc.status != "ready"
        ):
            raise ValidationError(
                "Policy coverage and existing indexing must be complete before publication."
            )
        if doc.retention_expires_at and doc.retention_expires_at <= timezone.now():
            raise PermissionDenied("Policy source retention has expired.")
        return doc, text, coverage, draft

    @classmethod
    @transaction.atomic
    def ensure_indexed(cls, user, policy):
        from apps.vector_search.models import DocumentChunk
        from apps.vector_search.services.chunking_service import ChunkingService
        from apps.vector_search.services.vector_indexing_service import (
            VectorIndexingService,
        )

        doc, text, coverage, _ = cls.source(user, policy)
        Document.objects.select_for_update().get(pk=doc.pk)
        doc.refresh_from_db()
        doc, text, coverage, _ = cls.source(user, policy)
        if not coverage["complete"] or doc.status != "ready":
            raise ValidationError("Policy source coverage is incomplete.")
        expected = ChunkingService.chunk_text(text)
        if len(expected) > 256:
            raise ValidationError("Policy index exceeds the bounded review limit.")
        actual = list(
            DocumentChunk.objects.filter(document=doc, is_deleted=False).order_by(
                "chunk_index"
            )[:257]
        )
        matches = len(actual) == len(expected) and all(
            saved.content == chunk["content"]
            and saved.metadata.get("start_char") == chunk["start_char"]
            and saved.metadata.get("end_char") == chunk["end_char"]
            for saved, chunk in zip(actual, expected)
        )
        if matches and doc.is_indexed:
            return {"indexed": True, "chunk_count": len(actual), "reused": True}
        result = VectorIndexingService.index_document(doc)
        return {**result, "reused": False}

    @classmethod
    @transaction.atomic
    def publish(cls, run):
        from apps.tenants.models import Company

        Company.objects.select_for_update().get(pk=run.company_id)
        policy = HRPolicy.objects.select_for_update().get(
            uuid=run.metadata["hr_policy_uuid"],
            company_id=run.company_id,
            is_deleted=False,
        )
        existing = HRPolicyVersion.objects.filter(workflow_run=run).first()
        if existing:
            return cls.authorize(run.initiated_by, existing, "ai")
        if policy.review_run_id != run.pk or policy.revision != run.metadata.get(
            "policy_revision"
        ):
            raise WorkflowConflict("Policy review is stale.")
        if not run.step_runs.filter(
            step__key="approve_policy", status="completed", approval__status="approved"
        ).exists():
            raise PermissionDenied(
                "An existing workflow approval is required to publish."
            )
        doc, text, coverage, draft = cls.source(run.initiated_by, policy, complete=True)
        from apps.workflows.models import WorkflowArtifact

        prepared = (
            WorkflowArtifact.objects.filter(
                run=run, step_run__step__key="prepare_policy", kind="processing_result"
            )
            .latest("created_at")
            .data
        )
        if (
            prepared.get("source_digest") != digest(text)
            or prepared.get("policy_revision") != policy.revision
            or prepared.get("governance_revision") != doc.governance_revision
        ):
            raise WorkflowConflict("Approved policy source changed.")
        cls.ensure_indexed(run.initiated_by, policy)
        start = date.fromisoformat(draft["effective_from"])
        end = (
            date.fromisoformat(draft["effective_until"])
            if draft.get("effective_until")
            else None
        )
        prior = (
            HRPolicyVersion.objects.filter(
                policy=policy, uuid=draft.get("supersedes")
            ).first()
            if draft.get("supersedes")
            else None
        )
        if prior:
            cls.authorize(run.initiated_by, prior)
        if draft.get("supersedes") and (
            not prior
            or prior.effective_from >= start
            or prior.publication.status != "published"
            or prior.replacements.filter(publication__status="published").exists()
        ):
            raise ValidationError(
                "Supersession must name an earlier published version of this policy."
            )
        for other in HRPolicyVersion.objects.filter(
            policy=policy, publication__status="published"
        ):
            if (
                other.pk != getattr(prior, "pk", None)
                and other.effective_from < (end or date.max)
                and start < (other.effective_until or date.max)
            ):
                replacements = (
                    other.replacements.filter(publication__status="published")
                    .order_by("effective_from")
                    .first()
                )
                if not replacements or start < replacements.effective_from:
                    raise ValidationError(
                        "Overlapping versions require explicit supersession."
                    )
        version = HRPolicyVersion.objects.create(
            policy=policy,
            number=(policy.versions.aggregate(n=Max("number"))["n"] or 0) + 1,
            title=policy.title,
            category=policy.category,
            department=policy.department,
            document=doc,
            source_digest=digest(text),
            governance_revision=doc.governance_revision,
            effective_from=start,
            effective_until=end,
            scope={k: draft[k] for k in ("employee_categories", "locations")},
            priority=draft["priority"],
            supersedes=prior,
            workflow_run=run,
            coverage=coverage,
            published_by=run.initiated_by,
        )
        HRPolicyPublication.objects.create(version=version)
        # Offset proofs reference the canonical extraction; policy text is not copied.
        matches = list(
            re.finditer(
                r"(?m)^(?:\d+(?:\.\d+)*[.)]?\s+|#{1,6}\s+)([^\n]{1,190})$", text
            )
        )
        starts = [(0, "Policy source")] + [
            (m.start(), m[1].strip()) for m in matches if m.start() > 0
        ]
        if len(starts) > 128:
            raise ValidationError("Policy section count exceeds the review limit.")
        for i, (offset, heading) in enumerate(starts):
            stop = starts[i + 1][0] if i + 1 < len(starts) else len(text)
            HRPolicySection.objects.create(
                version=version,
                position=i,
                heading=heading,
                evidence=evidence(doc, text, offset, stop),
                digest=digest(text[offset:stop]),
            )
        event(run.initiated_by, version, "published")
        return version

    @classmethod
    @transaction.atomic
    def retire(cls, user, version, revision):
        from apps.tenants.models import Company

        Company.objects.select_for_update().get(pk=user.company_id)
        version = cls.authorize(user, version)
        WorkflowDefinitionService.require_admin(user, version.department_id)
        state = HRPolicyPublication.objects.select_for_update().get(version=version)
        if state.revision != revision:
            raise WorkflowConflict()
        if state.status != "retired":
            state.status = "retired"
            state.revision += 1
            state.retired_at = timezone.now()
            state.retired_by = user
            state.save()
            event(user, version, "retired")
        return state

    @classmethod
    def compare(cls, user, left, right):
        left, right = cls.authorize(user, left), cls.authorize(user, right)
        if left.policy_id != right.policy_id:
            raise ValidationError("Compare two versions of the same policy.")

        def sections(version):
            return {
                s.position: {
                    "heading": s.heading,
                    "digest": s.digest,
                    "evidence": s.evidence,
                }
                for s in version.sections.all()
            }

        a, b = sections(left), sections(right)
        changes = [
            {
                "position": p,
                "kind": "added"
                if p not in a
                else "removed"
                if p not in b
                else "wording_changed",
                "before": a.get(p),
                "after": b.get(p),
            }
            for p in sorted(set(a) | set(b))
            if p not in a
            or p not in b
            or (a[p]["digest"], a[p]["heading"]) != (b[p]["digest"], b[p]["heading"])
        ]
        event(user, right, "versions_compared")
        return {
            "left_version_uuid": str(left.uuid),
            "right_version_uuid": str(right.uuid),
            "changes": changes,
            "effective_dates": {
                "before": [left.effective_from, left.effective_until],
                "after": [right.effective_from, right.effective_until],
            },
            "interpretation": "Source changes; no automatic contradiction or legal conclusion.",
        }


class HRPolicyResolver:
    @classmethod
    def resolve(
        cls,
        user,
        *,
        as_of=None,
        category=None,
        employee_category=None,
        location=None,
        department=None,
        version_uuids=None,
    ):
        HRPolicyService.fresh(user)
        day = as_of or timezone.localdate()
        if isinstance(day, str):
            try:
                day = date.fromisoformat(day)
            except ValueError:
                raise ValidationError("Invalid policy as-of date.")
        if department and not WorkflowPermissionService.department_allowed(
            user, department
        ):
            raise PermissionDenied("Department scope is unavailable.")
        for value, kind in ((employee_category, "employee"), (location, "location")):
            if value and not categories(user, kind).filter(code=value).exists():
                raise ValidationError("Unconfigured policy applicability value.")
        qs = (
            HRPolicyVersion.objects.filter(
                policy__in=HRPolicyService.visible(user), is_deleted=False
            )
            .select_related(
                "policy", "document__text_extraction", "publication", "category"
            )
            .prefetch_related("replacements__publication")
        )
        if category:
            qs = qs.filter(category__code=category)
        if version_uuids:
            qs = qs.filter(uuid__in=version_uuids)
        else:
            state = Q(publication__status="published")
            if as_of:
                state |= Q(
                    publication__status="retired", publication__retired_at__date__gt=day
                )
            qs = qs.filter(state, effective_from__lte=day).filter(
                Q(effective_until__isnull=True) | Q(effective_until__gt=day)
            )
        allowed_documents = DocumentPermissionService.get_accessible_document_queryset(
            user, require_ai=True, scope="internal"
        )
        qs = qs.filter(
            document__in=allowed_documents,
            category__is_active=True,
            category__is_deleted=False,
        )
        if not DocumentPermissionService.is_company_admin(user):
            qs = qs.filter(
                Q(department__isnull=True)
                | Q(
                    department_id__in=DocumentPermissionService.get_user_department_ids(
                        user
                    )
                )
            )
        qs = qs.filter(
            Q(department__isnull=True)
            | Q(department__is_active=True, department__is_deleted=False)
        )
        candidates = list(qs[:101])
        if len(candidates) > 100:
            return {
                "status": "review_required",
                "reason": "policy_scope_too_broad",
                "versions": [],
                "as_of": day,
                "mode": "historical" if as_of or version_uuids else "current",
            }
        selected, missing = [], False
        for candidate in candidates:
            version = candidate
            text, coverage = ClauseDetectionService.source(version.document)
            if (
                not coverage["complete"]
                or digest(text) != version.source_digest
                or version.document.governance_revision != version.governance_revision
            ):
                continue
            if department and version.department_id not in (None, department):
                continue
            if not version_uuids and any(
                r.effective_from <= day for r in candidate.replacements.all()
            ):
                continue
            eligible = True
            for key, supplied in (
                ("employee_categories", employee_category),
                ("locations", location),
            ):
                configured = version.scope.get(key, [])
                if configured and not supplied:
                    missing, eligible = True, False
                elif configured and supplied not in configured:
                    eligible = False
            if eligible:
                selected.append(version)
        status, reason = "resolved", None
        if missing and not version_uuids:
            status, reason, selected = (
                "review_required",
                "missing_applicability_context",
                [],
            )
        if not version_uuids and selected:
            settings = HRPolicySettings.objects.filter(
                company=user.company, is_deleted=False
            ).first()
            dimensions = settings.precedence if settings else []

            def rank(v):
                values = {
                    "priority": v.priority,
                    "department": int(v.department_id is not None),
                    "employee_category": int(bool(v.scope.get("employee_categories"))),
                    "location": int(bool(v.scope.get("locations"))),
                }
                return tuple(values[k] for k in dimensions)

            grouped = {}
            for v in selected:
                grouped.setdefault(v.category_id, []).append(v)
            selected = []
            for group in grouped.values():
                best = max(rank(v) for v in group)
                winners = [v for v in group if rank(v) == best]
                if len(winners) != 1:
                    status, reason, selected = (
                        "review_required",
                        "ambiguous_policy_scope",
                        [],
                    )
                    break
                selected.extend(winners)
        if not selected and status == "resolved":
            status, reason = "abstained", "no_authorized_effective_policy"
        if version_uuids and len(selected) != len(version_uuids):
            status, reason, selected = (
                "abstained",
                "requested_policy_version_unavailable",
                [],
            )
        return {
            "status": status,
            "reason": reason,
            "versions": selected,
            "as_of": day,
            "mode": "historical" if as_of or version_uuids else "current",
        }
