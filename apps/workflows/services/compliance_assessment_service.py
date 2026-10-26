from collections import Counter

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.tenants.models import Company
from apps.workflows.models import WorkflowFinding, WorkflowComment
from apps.workflows.models.compliance import (
    ComplianceAssessment,
    ComplianceEvidence,
    ComplianceEvaluation,
    ComplianceDecision,
    ComplianceOutput,
    ComplianceFrameworkVersion,
    ComplianceControlMapping,
)
from apps.workflows.permissions.workflow_permissions import WorkflowPermissionService
from apps.workflows.services import compliance_schema as schema
from apps.workflows.services.compliance_evidence_service import (
    ComplianceEvidenceService as evidence,
)
from apps.workflows.services.compliance_library_service import (
    ComplianceLibraryService as library,
    event,
)
from apps.workflows.services.finance_audit_service import digest
from apps.workflows.services.workflow_definition_service import (
    WorkflowDefinitionService,
)
from apps.workflows.services.workflow_engine_service import (
    WorkflowEngineService,
    WorkflowConflict,
)
from apps.workflows.services.workflow_events import notify_workflow


class ComplianceAssessmentService:
    @staticmethod
    def visible(user):
        if not WorkflowPermissionService.active(user):
            return ComplianceAssessment.objects.none()
        qs = ComplianceAssessment.objects.filter(
            company_id=user.company_id, is_deleted=False
        ).select_related("company", "run", "framework_version__framework")
        if not user.role == "company_admin" and not user.is_superuser:
            from apps.documents.services import DocumentPermissionService

            qs = qs.filter(
                Q(department__isnull=True)
                | Q(
                    department_id__in=DocumentPermissionService.get_user_department_ids(
                        user
                    )
                )
            )
        return qs

    @classmethod
    def authorize(cls, user, assessment, action="view"):
        # Reload the principal on every derivative access, including worker commands.
        user.refresh_from_db()
        if not cls.visible(user).filter(
            pk=assessment.pk
        ).exists() or not WorkflowPermissionService.department_allowed(
            user, assessment.department_id
        ):
            raise PermissionDenied("Compliance assessment is unavailable.")
        evidence.authorize(user, assessment, action)
        return assessment

    @classmethod
    def locked(cls, user, assessment, revision=None):
        snapshot = ComplianceAssessment.objects.get(pk=assessment.pk)
        if snapshot.run_id:
            WorkflowEngineService.locked(snapshot.run)
        Company.objects.select_for_update().get(pk=snapshot.company_id)
        assessment = ComplianceAssessment.objects.select_for_update().get(
            pk=snapshot.pk
        )
        if assessment.run_id != snapshot.run_id or (
            revision is not None and revision != assessment.revision
        ):
            raise WorkflowConflict()
        return cls.authorize(user, assessment)

    @staticmethod
    def editor(user, assessment, before_start=False):
        if not assessment.run_id:
            if before_start:
                WorkflowDefinitionService.require_admin(user, assessment.department_id)
                return
            raise WorkflowConflict("Assessment review has not started.")
        if (
            assessment.run.status != "review_required"
            or assessment.run.current_step.key != "review_assessment"
        ):
            raise WorkflowConflict(
                "Compliance changes require the active assessment review."
            )
        step = assessment.run.step_runs.get(step=assessment.run.current_step)
        WorkflowPermissionService.require(user, assessment.run, "review", step)

    @staticmethod
    def changed(assessment, source=False):
        assessment.revision += 1
        if source:
            assessment.evidence_revision += 1
        assessment.save(update_fields=["revision", "evidence_revision", "updated_at"])
        if (
            assessment.run_id
            and assessment.run.status not in WorkflowEngineService.CLOSED
        ):
            assessment.run.revision += 1
            assessment.run.metadata = {
                **assessment.run.metadata,
                "assessment_revision": assessment.revision,
            }
            assessment.run.save(update_fields=["revision", "metadata", "updated_at"])

    @staticmethod
    def replay(query, key, payload):
        schema.text(key, 128)
        previous = query.filter(idempotency_key=key).first()
        if previous and previous.command_digest != digest(payload):
            raise WorkflowConflict("Idempotency key was used with different input.")
        return previous

    @classmethod
    @transaction.atomic
    def create(
        cls,
        user,
        *,
        title,
        framework_version_uuid,
        scope,
        idempotency_key,
        department=None,
        previous_uuid=None,
    ):
        WorkflowDefinitionService.require_admin(user, department)
        Company.objects.select_for_update().get(pk=user.company_id)
        scope = schema.scope(scope)
        payload = {
            "title": schema.text(title),
            "version": schema.uuid(framework_version_uuid),
            "scope": scope,
            "department": department,
            "previous": schema.uuid(previous_uuid) if previous_uuid else None,
        }
        old = cls.replay(
            ComplianceAssessment.objects.filter(company_id=user.company_id),
            idempotency_key,
            payload,
        )
        if old:
            return cls.authorize(user, old)
        version = (
            ComplianceFrameworkVersion.objects.filter(
                uuid=framework_version_uuid,
                framework__in=library.frameworks(user),
                is_deleted=False,
            )
            .select_related("framework")
            .first()
        )
        if (
            not version
            or not version.framework.is_active
            or version.framework.active_version_id != version.pk
        ):
            raise ValidationError(
                "New assessments require an active published framework version."
            )
        previous = (
            cls.visible(user).filter(uuid=previous_uuid).first()
            if previous_uuid
            else None
        )
        if previous_uuid:
            if (
                not previous
                or previous.department_id != department
                or previous.framework_version.framework_id != version.framework_id
            ):
                raise PermissionDenied(
                    "Reassessment lineage is unavailable in this scope."
                )
            # Lineage references identifiers only; expired old evidence is not copied.
            if not WorkflowPermissionService.department_allowed(
                user, previous.department_id
            ):
                raise PermissionDenied("Reassessment lineage is unavailable.")
            if (
                not previous.run_id
                or previous.run.status not in WorkflowEngineService.CLOSED
            ):
                raise WorkflowConflict(
                    "Close the previous assessment before reassessment."
                )
        requirements = list(
            version.requirements.select_related("parent").order_by("code")
        )
        controls = library.controls(user).filter(
            is_active=True, definition__status="active"
        )
        # Company-wide assessments cannot capture department-private controls.
        controls = (
            controls.filter(Q(department__isnull=True) | Q(department_id=department))
            if department
            else controls.filter(department__isnull=True)
        )
        if scope["control_uuids"]:
            controls = controls.filter(uuid__in=scope["control_uuids"])
            if controls.count() != len(scope["control_uuids"]):
                raise PermissionDenied("Control scope is unavailable.")
        mappings = list(
            ComplianceControlMapping.objects.filter(
                company_id=user.company_id,
                requirement__version=version,
                control__in=controls,
                is_active=True,
                is_deleted=False,
            )
            .select_related("control", "requirement")
            .order_by("pk")[:1001]
        )
        if len(mappings) > 1000:
            raise ValidationError("Assessment mapping exceeds 1000 entries.")
        snapshot = {
            "framework": version.metadata,
            "framework_digest": version.digest,
            "requirements": [
                {
                    "uuid": str(r.uuid),
                    "code": r.code,
                    "title": r.title,
                    "category": r.category,
                    "type": r.requirement_type,
                    "obligation": r.obligation,
                    "parent": str(r.parent.uuid) if r.parent else None,
                    "applicability": r.applicability,
                    "source": r.source,
                }
                for r in requirements
            ],
            "mappings": [
                {
                    "requirement_uuid": str(m.requirement.uuid),
                    "control_uuid": str(m.control.uuid),
                    "control_revision": m.control.revision,
                    "code": m.control.code,
                    "name": m.control.name,
                    "department": m.control.department_id,
                    "definition": m.control.definition,
                }
                for m in mappings
            ],
        }
        evidence.documents(user, scope)
        assessment = ComplianceAssessment.objects.create(
            company=user.company,
            department_id=department,
            title=title,
            framework_version=version,
            scope=scope,
            snapshot=snapshot,
            previous=previous,
            created_by=user,
            idempotency_key=idempotency_key,
            command_digest=digest(payload),
        )
        event(
            user,
            assessment,
            "reassessment_created" if previous else "assessment_created",
        )
        return assessment

    @staticmethod
    def mapping(assessment, requirement_uuid, control_uuid):
        requirement_uuid, control_uuid = (
            schema.uuid(requirement_uuid),
            schema.uuid(control_uuid),
        )
        if not any(
            m["requirement_uuid"] == requirement_uuid
            and m["control_uuid"] == control_uuid
            for m in assessment.snapshot["mappings"]
        ):
            raise ValidationError(
                "Evidence target is not in the frozen assessment mapping."
            )
        return requirement_uuid, control_uuid

    @classmethod
    @transaction.atomic
    def link(
        cls,
        user,
        assessment,
        *,
        revision,
        requirement_uuid,
        control_uuid,
        source_kind,
        source_uuid,
        idempotency_key,
        dates=None,
        location=None,
        externally_shareable=False,
    ):
        assessment = cls.locked(user, assessment)
        req, ctrl = cls.mapping(assessment, requirement_uuid, control_uuid)
        dates = schema.dates({} if dates is None else dates)
        location = schema.obj(
            {} if location is None else location, {"start", "end", "quote", "reference"}
        )
        if type(externally_shareable) is not bool:
            raise ValidationError("External disclosure switch must be boolean.")
        payload = {
            "requirement": req,
            "control": ctrl,
            "kind": schema.choice(source_kind, schema.SOURCE_KINDS),
            "source": schema.uuid(source_uuid),
            "dates": dates,
            "location": location,
            "external": externally_shareable,
        }
        old = cls.replay(assessment.evidence, idempotency_key, payload)
        if old:
            return old
        if revision != assessment.revision:
            raise WorkflowConflict()
        cls.editor(user, assessment, True)
        if assessment.evidence.count() >= 200:
            raise ValidationError("Assessment evidence limit reached.")
        pin, _ = evidence.resolve(user, assessment, source_kind, source_uuid)
        if location:
            if source_kind != "document" or set(location) != {"start", "end", "quote"}:
                raise ValidationError(
                    "Locations require exact document source offsets and quote."
                )
            from apps.documents.models import Document
            from apps.workflows.services.clause_detection_service import (
                ClauseDetectionService,
            )

            doc = Document.objects.get(uuid=source_uuid, company=user.company)
            text, coverage = ClauseDetectionService.source(doc)
            start, end = location["start"], location["end"]
            if (
                not coverage["complete"]
                or type(start) is not int
                or type(end) is not int
                or not 0 <= start < end <= len(text)
                or text[start:end] != location["quote"]
                or end - start > 2000
            ):
                raise ValidationError(
                    "Evidence quote does not match the pinned source."
                )
        result = ComplianceEvidence.objects.create(
            assessment=assessment,
            requirement_id=assessment.framework_version.requirements.get(uuid=req).pk,
            control_id=library.controls(user).get(uuid=ctrl).pk,
            source_kind=source_kind,
            source_uuid=source_uuid,
            pin=pin,
            dates=dates,
            location=location,
            externally_shareable=externally_shareable,
            actor=user,
            idempotency_key=idempotency_key,
            command_digest=digest(payload),
        )
        cls.changed(assessment, True)
        event(user, assessment, "evidence_linked")
        return result

    @staticmethod
    def latest_decisions(assessment, kind, evaluation=None):
        qs = assessment.decisions.filter(kind=kind)
        if evaluation is not None:
            qs = qs.filter(evaluation=evaluation)
        return {d.target: d for d in qs.order_by("pk")}

    @classmethod
    def evidence_states(cls, user, assessment):
        decisions = cls.latest_decisions(assessment, "evidence")
        result = {}
        resolved = {}
        for link in assessment.evidence.select_related(
            "requirement", "control"
        ).order_by("pk"):
            decision = decisions.get(str(link.uuid))
            key = (link.source_kind, link.source_uuid)
            if key not in resolved:
                resolved[key] = evidence.resolve(user, assessment, *key)
            state = evidence.validity(
                user,
                assessment,
                link,
                bool(decision and decision.outcome == "valid"),
                resolved=resolved[key],
            )
            if decision and decision.outcome == "revoked":
                state = "revoked"
            elif decision and decision.outcome == "insufficient":
                state = "insufficient"
            result[str(link.uuid)] = {
                "uuid": str(link.uuid),
                "requirement_uuid": str(link.requirement.uuid),
                "control_uuid": str(link.control.uuid),
                "source_kind": link.source_kind,
                "source_uuid": str(link.source_uuid),
                "pin": link.pin,
                "dates": link.dates,
                "location": link.location,
                "state": state,
                "review_uuid": str(decision.uuid) if decision else None,
                "externally_shareable": link.externally_shareable,
                "collected_at": link.created_at.isoformat(),
                "collected_by": link.actor_id,
            }
        return result

    @classmethod
    def basis(cls, user, assessment):
        states = cls.evidence_states(user, assessment)
        applicability = cls.latest_decisions(assessment, "applicability")
        today = timezone.localdate().isoformat()
        return (
            digest(
                {
                    "evidence_revision": assessment.evidence_revision,
                    "evidence": states,
                    "applicability": {k: str(v.uuid) for k, v in applicability.items()},
                    "control_effective": {
                        m["control_uuid"]: m["definition"].get(
                            "effective_from", "0001-01-01"
                        )
                        <= today
                        <= m["definition"].get("effective_until", "9999-12-31")
                        for m in assessment.snapshot["mappings"]
                    },
                }
            ),
            states,
            applicability,
        )

    @classmethod
    @transaction.atomic
    def evaluate(cls, user, assessment, *, revision, idempotency_key, internal=False):
        assessment = cls.locked(user, assessment)
        schema.text(idempotency_key, 128)
        basis, states, applicable = cls.basis(user, assessment)
        old = assessment.evaluations.filter(idempotency_key=idempotency_key).first()
        if old:
            if old.fingerprint != basis:
                raise WorkflowConflict("Evaluation command has a stale evidence basis.")
            return old
        if revision != assessment.revision:
            raise WorkflowConflict()
        if not internal:
            cls.editor(user, assessment)
        elif (
            not assessment.run_id
            or assessment.run.status != "processing"
            or assessment.run.current_step.key != "evaluate_controls"
        ):
            raise WorkflowConflict("Internal evaluation requires the processing step.")
        if assessment.evaluations.count() >= 50:
            raise ValidationError("Evaluation history limit reached; reassess instead.")
        rows = []
        today = timezone.localdate().isoformat()
        for req in assessment.snapshot["requirements"]:
            app = applicable.get(req["uuid"])
            state = (
                app.outcome
                if app
                else (
                    "undetermined"
                    if req["obligation"] == "conditional"
                    else "applicable"
                )
            )
            condition = req["applicability"]
            if not app and condition:
                dimension = assessment.scope[condition["dimension"]]
                state = (
                    "undetermined"
                    if not dimension
                    else (
                        "conditionally_applicable"
                        if set(dimension) & set(condition["values"])
                        else "undetermined"
                    )
                )
            mappings = [
                m
                for m in assessment.snapshot["mappings"]
                if m["requirement_uuid"] == req["uuid"]
            ]
            if not mappings:
                mappings = [
                    {
                        "control_uuid": None,
                        "definition": {"tests": ["unmapped_control"]},
                    }
                ]
            for mapping in mappings:
                ctrl = mapping["control_uuid"]
                links = [
                    v
                    for v in states.values()
                    if v["requirement_uuid"] == req["uuid"]
                    and v["control_uuid"] == ctrl
                ]
                valid = [v for v in links if v["state"] == "valid"]
                tests = mapping["definition"]["tests"]
                checks = {
                    "missing_evidence": bool(valid),
                    "expired_evidence": bool(links)
                    and all(
                        v["state"]
                        not in {"expired", "stale", "revoked", "insufficient"}
                        for v in links
                    ),
                    "missing_approval": bool(valid)
                    and all(v["pin"]["approved"] for v in valid),
                    "overdue_review": bool(links)
                    and all(
                        v["dates"].get("review_due", "9999-12-31") >= today
                        for v in links
                    ),
                    "unmapped_control": ctrl is not None,
                }
                for date_key, comparison in (
                    ("effective_from", lambda d: d <= today),
                    ("effective_until", lambda d: d >= today),
                ):
                    if mapping["definition"].get(date_key) and not comparison(
                        mapping["definition"][date_key]
                    ):
                        checks["control_effective"] = False
                checks = {
                    k: v
                    for k, v in checks.items()
                    if k in tests or k == "control_effective"
                }
                failed = [key for key, passed in checks.items() if not passed]
                outcome = (
                    "not_applicable"
                    if state == "not_applicable"
                    else "review_required"
                    if not failed
                    else "insufficient_evidence"
                    if not valid
                    else "not_satisfied"
                )
                target = req["uuid"] + ":" + (ctrl or "unmapped")
                row = {
                    "target": target,
                    "requirement_uuid": req["uuid"],
                    "control_uuid": ctrl,
                    "applicability": state,
                    "outcome": outcome,
                    "checks": checks,
                    "evidence": [v["uuid"] for v in links],
                    "date_basis": today,
                    "finding_uuid": None,
                }
                if failed or state in {"undetermined", "conditionally_applicable"}:
                    step = assessment.run.step_runs.get(
                        step=assessment.run.current_step
                    )
                    finding = WorkflowFinding.objects.create(
                        run=assessment.run,
                        step_run=step,
                        category="compliance_control_gap",
                        severity=assessment.snapshot["framework"]["policy"]["severity"],
                        title="Compliance control requires review",
                        data={
                            "target": target,
                            "failed_tests": failed,
                            "date_basis": today,
                        },
                        evidence=[
                            {
                                "evidence_uuid": v["uuid"],
                                "source_uuid": v["source_uuid"],
                            }
                            for v in links
                        ],
                    )
                    row["finding_uuid"] = str(finding.uuid)
                rows.append(row)
        evaluation = ComplianceEvaluation.objects.create(
            assessment=assessment,
            run=assessment.run,
            evidence_revision=assessment.evidence_revision,
            fingerprint=basis,
            data={
                "rows": rows,
                "counts": dict(Counter(r["outcome"] for r in rows)),
                "evidence": states,
                "date_basis": today,
            },
            idempotency_key=idempotency_key,
        )
        cls.changed(assessment)
        event(user, assessment, "controls_evaluated")
        return evaluation

    @classmethod
    @transaction.atomic
    def decide(
        cls,
        user,
        assessment,
        *,
        revision,
        kind,
        target,
        outcome,
        reason,
        idempotency_key,
        data=None,
    ):
        assessment = cls.locked(user, assessment)
        kind = schema.choice(
            kind,
            {
                "evidence",
                "applicability",
                "control",
                "evidence_request",
                "remediation",
                "risk_acceptance",
            },
        )
        data = schema.obj(
            {} if data is None else data,
            {"owner", "due", "request_uuid", "evidence_uuids", "expires", "review_due"},
        )
        payload = {
            "kind": kind,
            "target": schema.text(target, 128),
            "outcome": schema.text(outcome, 40),
            "reason": schema.text(reason, 2000),
            "data": data or {},
        }
        old = cls.replay(assessment.decisions, idempotency_key, payload)
        if old:
            return old
        if revision != assessment.revision:
            raise WorkflowConflict()
        cls.editor(user, assessment)
        if assessment.decisions.count() >= 1000:
            raise ValidationError("Decision history limit reached; reassess instead.")
        data = schema.obj(
            data or {},
            {"owner", "due", "request_uuid", "evidence_uuids", "expires", "review_due"},
        )
        for key in {"due", "expires", "review_due"} & set(data):
            schema.iso(data[key])
        if "owner" in data:
            WorkflowDefinitionService.validate_assignment(
                user.company_id, data["owner"]
            )
            owner_department = data["owner"].get("department")
            if owner_department and owner_department != assessment.department_id:
                raise PermissionDenied(
                    "Action owner must be in the assessment department."
                )
            if data["owner"].get("user"):
                from apps.accounts.models import User

                cls.authorize(User.objects.get(pk=data["owner"]["user"]), assessment)
        if data.get("evidence_uuids") is not None:
            ids = schema.strings(data["evidence_uuids"], 20, True)
            if assessment.evidence.filter(uuid__in=ids).count() != len(ids):
                raise ValidationError("Submitted evidence is outside this assessment.")
        evaluation = assessment.evaluations.order_by("-pk").first()
        row = None
        if kind == "evidence":
            schema.choice(outcome, {"valid", "revoked", "insufficient"})
            link = assessment.evidence.filter(uuid=schema.uuid(target)).first()
            if not link:
                raise ValidationError("Evidence target unavailable.")
            if (
                outcome == "valid"
                and evidence.validity(user, assessment, link, True) != "valid"
            ):
                raise ValidationError("Invalid evidence cannot be marked valid.")
        elif kind == "applicability":
            schema.choice(outcome, schema.APPLICABILITY)
            if not any(
                r["uuid"] == target for r in assessment.snapshot["requirements"]
            ):
                raise ValidationError("Requirement unavailable.")
            if outcome == "not_applicable":
                WorkflowDefinitionService.require_admin(user, assessment.department_id)
        else:
            if (
                not evaluation
                or evaluation.fingerprint != cls.basis(user, assessment)[0]
            ):
                raise WorkflowConflict(
                    "Evaluate the current evidence before reviewing controls."
                )
            row = next(
                (r for r in evaluation.data["rows"] if r["target"] == target), None
            )
            if not row:
                raise ValidationError("Control assessment target unavailable.")
            if kind == "control":
                schema.choice(outcome, schema.OUTCOMES)
                if outcome == "satisfied" and (
                    not row["control_uuid"]
                    or not row["evidence"]
                    or not all(row["checks"].values())
                    or row["applicability"] != "applicable"
                    or any(
                        evaluation.data["evidence"][ref]["state"] != "valid"
                        for ref in row["evidence"]
                    )
                ):
                    raise ValidationError(
                        "Satisfied requires applicable mapped controls and valid reviewed evidence with passing tests."
                    )
                if (
                    outcome == "not_applicable"
                    and row["applicability"] != "not_applicable"
                ):
                    raise ValidationError(
                        "Not applicable requires an explicit applicability decision."
                    )
            elif kind == "risk_acceptance":
                WorkflowDefinitionService.require_admin(user, assessment.department_id)
                schema.choice(outcome, {"accepted_risk", "revoked"})
                if (
                    not assessment.snapshot["framework"]["policy"][
                        "allow_risk_acceptance"
                    ]
                    or not assessment.run.findings.filter(data__target=target).exists()
                ):
                    raise PermissionDenied(
                        "Risk acceptance is not permitted for this control."
                    )
                if outcome == "accepted_risk" and (
                    not data.get("expires")
                    or data["expires"] <= timezone.localdate().isoformat()
                ):
                    raise ValidationError("Risk acceptance requires a future expiry.")
            else:
                schema.choice(
                    outcome,
                    {"open", "in_progress", "submitted", "resolved", "rejected"},
                )
                if not assessment.run.findings.filter(data__target=target).exists():
                    raise ValidationError("Requests and remediation require a finding.")
                if outcome == "open" and (not data.get("owner") or not data.get("due")):
                    raise ValidationError(
                        "Requests and remediation require owner and due date."
                    )
                if data.get("request_uuid"):
                    request = assessment.decisions.filter(
                        uuid=schema.uuid(data["request_uuid"]),
                        kind=kind,
                        target=target,
                        outcome="open",
                    ).first()
                    if not request:
                        raise ValidationError("Action lineage is unavailable.")
                elif outcome != "open":
                    raise ValidationError(
                        "Action updates require the original request UUID."
                    )
                if outcome == "submitted" and not data.get("evidence_uuids"):
                    raise ValidationError("Submission requires registered evidence.")
        decision = ComplianceDecision.objects.create(
            assessment=assessment,
            evaluation=evaluation
            if kind not in {"evidence", "applicability"}
            else None,
            actor=user,
            kind=kind,
            target=target,
            outcome=outcome,
            reason=reason,
            data={**data, "assessment_revision": revision},
            idempotency_key=idempotency_key,
            command_digest=digest(payload),
        )
        if (
            kind == "control"
            and outcome
            in {"partially_satisfied", "not_satisfied", "insufficient_evidence"}
            and not row["finding_uuid"]
        ):
            step = assessment.run.step_runs.get(step=assessment.run.current_step)
            WorkflowFinding.objects.create(
                run=assessment.run,
                step_run=step,
                category="compliance_review_gap",
                severity=assessment.snapshot["framework"]["policy"]["severity"],
                title="Reviewed compliance control gap",
                data={"target": target, "decision_uuid": str(decision.uuid)},
                evidence=[{"evidence_uuid": ref} for ref in row["evidence"]],
                status="accepted",
                review_state="reviewed",
            )
        if row and kind == "control":
            WorkflowFinding.objects.filter(
                run=assessment.run, data__target=target
            ).update(
                status="resolved"
                if outcome in {"satisfied", "not_applicable"}
                else "accepted",
                review_state="reviewed",
            )
        cls.changed(assessment, kind in {"evidence", "applicability"})
        WorkflowComment.objects.create(
            run=assessment.run,
            author=user,
            body="Compliance " + kind + " recorded: " + str(decision.uuid),
        )
        event(user, assessment, kind + "_reviewed")
        recipients = None
        if data.get("owner", {}).get("user"):
            from apps.accounts.models import User

            recipients = [
                assessment.run.initiated_by,
                User.objects.get(pk=data["owner"]["user"]),
            ]
        notify_workflow(
            assessment.run, user, "compliance_" + kind, recipients=recipients
        )
        return decision

    @classmethod
    def readiness(cls, user, assessment):
        cls.authorize(user, assessment)
        evaluation = assessment.evaluations.order_by("-pk").first()
        if not evaluation or evaluation.fingerprint != cls.basis(user, assessment)[0]:
            raise ValidationError(
                "A current completed compliance evaluation is required."
            )
        if assessment.run.exceptions.filter(status="open").exists():
            raise ValidationError("Compliance runtime exceptions remain unresolved.")
        if assessment.run.findings.filter(
            Q(status="open") | ~Q(review_state="reviewed")
        ).exists():
            raise ValidationError("Compliance findings require recorded human review.")
        decisions = cls.latest_decisions(assessment, "control", evaluation)
        risks = cls.latest_decisions(assessment, "risk_acceptance", evaluation)
        for row in evaluation.data["rows"]:
            decision = decisions.get(row["target"])
            if (
                not decision
                or decision.outcome == "review_required"
                or row["applicability"] in {"undetermined", "conditionally_applicable"}
            ):
                raise ValidationError(
                    "Every requirement and control requires a current human review."
                )
            if decision.outcome not in {"satisfied", "not_applicable"}:
                risk = risks.get(row["target"])
                if (
                    not risk
                    or risk.outcome != "accepted_risk"
                    or risk.data["expires"] <= timezone.localdate().isoformat()
                    or risk.data.get("review_due", "9999-12-31")
                    < timezone.localdate().isoformat()
                ):
                    raise ValidationError(
                        "Unresolved gaps require explicit current authorized risk acceptance."
                    )
        return evaluation

    @classmethod
    def summary(cls, user, assessment, evaluation=None):
        cls.authorize(user, assessment)
        evaluation = evaluation or assessment.evaluations.order_by("-pk").first()
        decisions = (
            cls.latest_decisions(assessment, "control", evaluation)
            if evaluation
            else {}
        )
        rows = (
            [
                {
                    **r,
                    "reviewed_outcome": decisions[r["target"]].outcome
                    if r["target"] in decisions
                    else None,
                    "review_uuid": str(decisions[r["target"]].uuid)
                    if r["target"] in decisions
                    else None,
                }
                for r in evaluation.data["rows"]
            ]
            if evaluation
            else []
        )
        current_basis, current_evidence, _ = cls.basis(user, assessment)
        current = bool(evaluation and evaluation.fingerprint == current_basis)
        if not current:
            for row in rows:
                row["historical_reviewed_outcome"] = row["reviewed_outcome"]
                row["reviewed_outcome"] = None
                row["outcome"] = "review_required"
        counts = dict(Counter(r["reviewed_outcome"] or r["outcome"] for r in rows))
        return {
            "assessment_uuid": str(assessment.uuid),
            "revision": assessment.revision,
            "evidence_revision": assessment.evidence_revision,
            "run_uuid": str(assessment.run.uuid) if assessment.run_id else None,
            "status": assessment.run.status if assessment.run_id else "draft",
            "framework_version_uuid": str(assessment.framework_version.uuid),
            "framework_version": assessment.framework_version.number,
            "framework": assessment.snapshot["framework"],
            "scope": assessment.scope,
            "requirements": assessment.snapshot["requirements"],
            "mappings": assessment.snapshot["mappings"],
            "evaluation_uuid": str(evaluation.uuid) if evaluation else None,
            "evaluation_current": current,
            "rows": rows,
            "counts": counts,
            "coverage": {
                "requirements": len(assessment.snapshot["requirements"]),
                "control_assessments": len(rows),
                "reviewed": len(decisions),
                "satisfied": counts.get("satisfied", 0),
            },
            "evidence": current_evidence,
            "assistance_only": True,
            "assurance": "Evidence-based internal assessment only. No certification, statutory assurance or legal compliance determination is provided.",
        }

    @classmethod
    @transaction.atomic
    def assemble(cls, assessment):
        assessment = cls.locked(assessment.run.initiated_by, assessment)
        old = assessment.outputs.filter(run=assessment.run).first()
        if old:
            return old
        approval_step = (
            assessment.run.step_runs.filter(step__key="approve_assessment")
            .select_related("approval")
            .first()
        )
        if (
            assessment.run.current_step.key != "finalize_assessment"
            or not approval_step
            or not approval_step.approval_id
            or approval_step.approval.status != "approved"
        ):
            raise WorkflowConflict("Compliance output requires workflow approval.")
        evaluation = cls.readiness(assessment.run.initiated_by, assessment)
        data = cls.summary(assessment.run.initiated_by, assessment, evaluation)
        data.update(
            {
                "schema_version": 1,
                "output_version": 1,
                "status": "completed",
                "approval_uuid": str(approval_step.approval.uuid),
                "previous_assessment_uuid": str(assessment.previous.uuid)
                if assessment.previous_id
                else None,
                "framework_digest": assessment.framework_version.digest,
                "history": [
                    {
                        "uuid": str(d.uuid),
                        "kind": d.kind,
                        "target": d.target,
                        "outcome": d.outcome,
                        "reason": d.reason,
                        "actor": d.actor_id,
                        "created_at": d.created_at.isoformat(),
                        "data": d.data,
                        "evaluation_uuid": str(d.evaluation.uuid)
                        if d.evaluation_id
                        else None,
                    }
                    for d in assessment.decisions.select_related("evaluation").order_by(
                        "pk"
                    )
                ],
                "findings": [
                    {
                        "uuid": str(f.uuid),
                        "category": f.category,
                        "severity": f.severity,
                        "status": f.status,
                        "data": f.data,
                        "evidence": f.evidence,
                    }
                    for f in assessment.run.findings.order_by("pk")
                ],
                "exceptions": [
                    {"uuid": str(e.uuid), "category": e.category, "status": e.status}
                    for e in assessment.run.exceptions.order_by("pk")
                ],
            }
        )
        output = ComplianceOutput.objects.create(
            assessment=assessment,
            run=assessment.run,
            evaluation=evaluation,
            data=data,
            digest=digest(data),
        )
        event(assessment.run.initiated_by, assessment, "output_finalized")
        return output

    @classmethod
    def export(cls, user, assessment, output, external=False):
        cls.authorize(user, assessment, "export")
        if (
            output.assessment_id != assessment.pk
            or output.run_id != assessment.run_id
            or output.run.status != "completed"
        ):
            raise WorkflowConflict("A finalized compliance output is required.")
        cls.readiness(user, assessment)
        if external:
            # A control-level share flag cannot declassify internal documents or reviewer comments.
            from apps.documents.models import Document

            if (
                not assessment.evidence.exists()
                or assessment.evidence.filter(externally_shareable=False).exists()
                or Document.objects.filter(uuid__in=assessment.scope["document_uuids"])
                .exclude(sensitivity_label="public")
                .exists()
            ):
                raise PermissionDenied(
                    "This compliance package lacks external disclosure authorization."
                )
            result = {
                key: output.data[key]
                for key in (
                    "schema_version",
                    "assistance_only",
                    "assurance",
                    "framework_version",
                    "framework_digest",
                    "counts",
                    "coverage",
                    "approval_uuid",
                )
            }
        else:
            result = output.data
        event(user, assessment, "output_exported")
        return {
            "output_uuid": str(output.uuid),
            "external": external,
            "digest": digest(result),
            "data": result,
        }
