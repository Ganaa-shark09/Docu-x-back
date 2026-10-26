import hashlib

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.documents.models import Document
from apps.documents.services import DocumentPermissionService
from apps.documents.services.field_permission_service import FieldPermissionService
from apps.workflows.models import (
    ClauseAnalysis,
    ClauseAnalysisOutput,
    ClauseInstance,
    ClauseCorrection,
    ClauseDeviation,
    ClauseReviewDecision,
    WorkflowFinding,
)
from apps.workflows.permissions.workflow_permissions import WorkflowPermissionService
from apps.workflows.services import clause_schema as validation
from apps.workflows.services.clause_detection_service import ClauseDetectionService
from apps.workflows.services.clause_playbook_service import ClausePlaybookService
from apps.workflows.services.workflow_engine_service import (
    WorkflowEngineService,
    WorkflowConflict,
    fingerprint,
)
from apps.workflows.services.workflow_events import workflow_event, notify_workflow


SAFE_COMPARISONS = {"matching", "preferred", "acceptable_fallback", "not_applicable"}


class ClauseAnalysisService:
    @staticmethod
    def digest(document):
        text, _ = ClauseDetectionService.source(document)
        return hashlib.sha256(text.encode()).hexdigest()

    @classmethod
    def authorize(cls, user, analysis, *, source=True):
        if not getattr(user, "is_authenticated", False):
            raise PermissionDenied(
                "Legal clause access requires an authenticated user."
            )
        user.refresh_from_db()
        run = analysis.workflow_run
        # Reload the principal source so cached model instances cannot bypass revocation.
        document = Document.objects.select_related("text_extraction").get(
            pk=analysis.document_id
        )
        if (
            document.company_id != analysis.company_id
            or run.company_id != analysis.company_id
            or analysis.playbook_version.is_deleted
            or not run.documents.filter(pk=document.pk).exists()
        ):
            raise PermissionDenied(
                "Legal analysis provenance is unavailable in this scope."
            )
        if analysis.contract_review_id and (
            analysis.contract_review.workflow_run_id != run.pk
            or analysis.contract_review.document_id != document.pk
        ):
            raise PermissionDenied(
                "Contract clause provenance is unavailable in this scope."
            )
        if (
            analysis.company_id != user.company_id
            or analysis.is_deleted
            or not WorkflowPermissionService.content_allowed(user, run)
        ):
            raise PermissionDenied(
                "Current permissions deny legal clause content access."
            )
        DocumentPermissionService.require_action(user, document, "view")
        if FieldPermissionService.has_restrictions(user, document, "view"):
            raise PermissionDenied(
                "Current clause field permissions deny content access."
            )
        ClausePlaybookService.require(user, analysis.playbook_version.playbook)
        if source and cls.digest(document) != analysis.source_digest:
            raise ValidationError("Source text changed; create a new legal analysis.")
        analysis.document = document
        if source and analysis.contract_review_id:
            analysis.contract_review.refresh_from_db()
            if cls.context(analysis.contract_review) != analysis.context:
                raise ValidationError(
                    "Contract context changed; create a new legal analysis."
                )

    @staticmethod
    def context(review=None):
        if not review:
            return {}
        return {
            k: review.normalized[k]
            for k in validation.CONTEXT
            if review.normalized.get(k) not in (None, "unknown", "ambiguous")
        }

    @classmethod
    @transaction.atomic
    def analyze(cls, run, step_run, options, contract_review=None):
        run = WorkflowEngineService.locked(run)
        existing = ClauseAnalysis.objects.filter(workflow_run=run).first()
        if existing:
            cls.authorize(run.initiated_by, existing)
            return existing
        document = run.documents.get()
        context = cls.context(contract_review)
        if document.document_type not in (None, "unknown", "ambiguous"):
            context.setdefault("contract_type", document.document_type)
        if contract_review:
            # Contract context is pinned to extracted facts, not request metadata.
            context = cls.context(contract_review)
        selected = ClausePlaybookService.select(
            run.initiated_by,
            department=run.department_id,
            document_type=context.get("contract_type") or document.document_type,
            explicit=options.get("playbook_version"),
        )
        previous = None
        if run.metadata.get("previous_clause_analysis"):
            previous = ClauseAnalysis.objects.filter(
                uuid=run.metadata["previous_clause_analysis"],
                company=run.company,
                document=document,
            ).first()
            if not previous:
                raise ValidationError("Re-analysis source is unavailable.")
            cls.authorize(run.initiated_by, previous, source=False)
        analysis = ClauseAnalysis.objects.create(
            company=run.company,
            workflow_run=run,
            document=document,
            contract_review=contract_review,
            playbook_version=selected,
            source_digest=cls.digest(document),
            context=context,
            previous_analysis=previous,
        )
        cls.authorize(run.initiated_by, analysis)
        DocumentPermissionService.require_action(run.initiated_by, document, "extract")
        if any(
            FieldPermissionService.has_restrictions(run.initiated_by, document, a)
            for a in ("view", "ai")
        ):
            raise PermissionDenied("Current field policies deny clause processing.")
        records, coverage = ClauseDetectionService.detect(
            analysis, step_run, options.get("use_ai", False)
        )
        cls.authorize(run.initiated_by, analysis)
        if options.get("use_ai"):
            DocumentPermissionService.require_action(
                run.initiated_by, analysis.document, "ai"
            )
            if FieldPermissionService.has_restrictions(
                run.initiated_by, analysis.document, "ai"
            ):
                raise PermissionDenied(
                    "Current field policies deny clause AI persistence."
                )
        seen = {}
        for record in records:
            ClauseDetectionService.validate_record(document, record, selected.taxonomy)
            identity = fingerprint(
                {
                    "type": record["clause_type"],
                    "heading": record["heading"],
                    "evidence": record["evidence"],
                }
            )
            if identity in seen:
                if seen[identity] != fingerprint(record["facts"]):
                    raise ValidationError(
                        "Conflicting facts for the same clause source require re-analysis."
                    )
                continue
            seen[identity] = fingerprint(record["facts"])
            ClauseInstance.objects.create(
                analysis=analysis,
                clause_type_id=cls.type_id(record["clause_type"]),
                identity=identity,
                heading=record["heading"],
                facts=record["facts"],
                evidence=record["evidence"],
                confidence=record["confidence"],
                detection_method=record["detection_method"],
            )
        analysis.coverage, analysis.analyzed_at = coverage, timezone.now()
        analysis.save(update_fields=["coverage", "analyzed_at", "updated_at"])
        cls.compare(analysis, step_run)
        workflow_event(
            run,
            run.initiated_by,
            "clause_analysis_created",
            analysis_uuid=str(analysis.uuid),
            playbook_version_uuid=str(selected.uuid),
            revision=analysis.revision,
        )
        return analysis

    @staticmethod
    def type_id(uuid):
        from apps.workflows.models import ClauseType

        return ClauseType.objects.get(uuid=uuid).pk

    @staticmethod
    def current(clause):
        corrected = next(iter(clause.corrections.all()), None)
        current = corrected or clause
        return {
            "uuid": str(clause.uuid),
            "clause_type": str(current.clause_type.uuid),
            "heading": clause.heading,
            "facts": current.facts,
            "evidence": current.evidence,
            "confidence": str(clause.confidence)
            if clause.confidence is not None
            else None,
            "detection_method": clause.detection_method,
            "correction_uuid": str(corrected.uuid) if corrected else None,
        }

    @classmethod
    def clauses(cls, analysis):
        rows = [
            cls.current(c)
            for c in analysis.clauses.select_related("clause_type")
            .prefetch_related("corrections__clause_type")
            .all()
        ]
        decisions = {}
        for decision in analysis.decisions.filter(
            revision=analysis.revision, target_type="clause"
        ):
            decisions.setdefault(str(decision.target), decision.decision)
        for row in rows:
            row["review_state"] = decisions.get(row["uuid"], "unreviewed")
        return rows

    @classmethod
    def compare(cls, analysis, step_run):
        rows = cls.clauses(analysis)
        latest = {}
        for decision in analysis.decisions.filter(
            revision=analysis.revision, target_type="clause"
        ):
            latest.setdefault(str(decision.target), decision.decision)
        rows = [row for row in rows if latest.get(row["uuid"]) != "dismissed"]
        clause_ids = {str(c.uuid): c.pk for c in analysis.clauses.all()}
        existing = set(
            analysis.deviations.filter(revision=analysis.revision).values_list(
                "identity", flat=True
            )
        )
        jobs = [
            (rule, row)
            for rule in analysis.playbook_version.rules
            for row in [r for r in rows if r["clause_type"] == rule["clause_type"]]
            or [None]
        ]
        if len(jobs) > 256:
            raise ValidationError(
                "Legal comparison exceeds the supported analysis scope."
            )
        for rule in analysis.playbook_version.rules:
            detected = [
                row for row in rows if row["clause_type"] == rule["clause_type"]
            ]
            for row in detected or [None]:
                observed = {
                    "context": analysis.context,
                    "facts": {k: f["value"] for k, f in row["facts"].items()}
                    if row
                    else {},
                }
                status = validation.comparison(
                    rule,
                    observed,
                    analysis.playbook_version.taxonomy[rule["clause_type"]][
                        "fact_schema"
                    ],
                    present=row is not None,
                    complete=analysis.coverage.get("complete", False),
                )
                identity = rule["code"] + ":" + (row["uuid"] if row else "absence")
                if identity in existing:
                    continue
                finding = None
                if status not in SAFE_COMPARISONS:
                    finding = WorkflowFinding.objects.create(
                        run=analysis.workflow_run,
                        step_run=step_run,
                        category="clause_deviation",
                        severity=rule["severity"],
                        title="Legal playbook review required",
                        description="Source-grounded clause comparison requires review against the pinned organization policy.",
                        evidence=row["evidence"] if row else [],
                        data={
                            "clause_analysis_uuid": str(analysis.uuid),
                            "clause_revision": analysis.revision,
                            "rule_code": rule["code"],
                            "comparison": status,
                            "coverage_complete": analysis.coverage.get(
                                "complete", False
                            ),
                        },
                    )
                ClauseDeviation.objects.create(
                    analysis=analysis,
                    clause_id=clause_ids[row["uuid"]] if row else None,
                    revision=analysis.revision,
                    identity=identity,
                    rule_code=rule["code"],
                    comparison=status,
                    severity=rule["severity"]
                    if status not in SAFE_COMPARISONS
                    else "info",
                    observed=observed,
                    expected=rule,
                    evidence=row["evidence"] if row else [],
                    requires_approval=rule["requires_approval"]
                    and status not in SAFE_COMPARISONS,
                    finding=finding,
                )

    @classmethod
    def snapshot(cls, analysis):
        return {
            "schema_version": 1,
            "analysis_uuid": str(analysis.uuid),
            "workflow_uuid": str(analysis.workflow_run.uuid),
            "document_uuid": str(analysis.document.uuid),
            "revision": analysis.revision,
            "source_digest": analysis.source_digest,
            "playbook_version_uuid": str(analysis.playbook_version.uuid),
            "coverage": analysis.coverage,
            "context": analysis.context,
            "clauses": cls.clauses(analysis),
            "deviations": list(
                analysis.deviations.filter(revision=analysis.revision).values(
                    "uuid",
                    "clause__uuid",
                    "rule_code",
                    "comparison",
                    "severity",
                    "observed",
                    "expected",
                    "evidence",
                    "requires_approval",
                )
            ),
            "decisions": list(
                analysis.decisions.filter(revision=analysis.revision).values(
                    "target",
                    "target_type",
                    "decision",
                    "actor_id",
                    "reason",
                    "created_at",
                )
            ),
        }

    @staticmethod
    def reason(value):
        if not isinstance(value, str) or not value.strip() or len(value) > 4000:
            raise ValidationError("A bounded review reason is required.")

    @classmethod
    def locked_review(cls, user, analysis, expected_revision):
        run = WorkflowEngineService.locked(analysis.workflow_run)
        analysis = ClauseAnalysis.objects.select_for_update().get(pk=analysis.pk)
        cls.authorize(user, analysis)
        step = run.step_runs.get(step=run.current_step)
        WorkflowPermissionService.require(user, run, "review", step)
        if (
            run.status not in ("review_required", "exception")
            or step.status != "waiting"
        ):
            raise ValidationError("Clause decisions require an open workflow review.")
        if expected_revision != run.revision:
            raise WorkflowConflict()
        return run, analysis, step

    @classmethod
    @transaction.atomic
    def decide(
        cls,
        user,
        analysis,
        *,
        target,
        target_type,
        decision,
        reason,
        expected_revision,
        idempotency_key,
    ):
        run = WorkflowEngineService.locked(analysis.workflow_run)
        cls.authorize(user, analysis)
        cls.reason(reason)
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
            raise ValidationError("A bounded idempotency key is required.")
        request_hash = fingerprint(
            {
                "actor": user.pk,
                "target": str(target),
                "target_type": target_type,
                "decision": decision,
                "reason": reason,
                "expected_revision": expected_revision,
            }
        )
        previous = analysis.decisions.filter(idempotency_key=idempotency_key).first()
        if previous:
            if previous.fingerprint != request_hash:
                raise WorkflowConflict()
            return previous
        run, analysis, step = cls.locked_review(user, analysis, expected_revision)
        if decision not in validation.DECISIONS or target_type not in (
            "clause",
            "deviation",
        ):
            raise ValidationError("Unsupported legal review decision.")
        obj = (
            analysis.clauses.filter(uuid=target).first()
            if target_type == "clause"
            else analysis.deviations.filter(
                uuid=target, revision=analysis.revision
            ).first()
        )
        if (
            not obj
            or target_type == "clause"
            and decision not in ("confirmed", "dismissed", "needs_more_information")
            or target_type == "deviation"
            and decision == "confirmed"
        ):
            raise ValidationError("Decision target or transition is invalid.")
        if target_type == "clause":
            current_decision = analysis.decisions.filter(
                revision=analysis.revision, target=target, target_type="clause"
            ).first()
            if (
                decision == "dismissed"
                or current_decision
                and current_decision.decision == "dismissed"
            ):
                analysis.revision += 1
                analysis.save(update_fields=["revision", "updated_at"])
        result = ClauseReviewDecision.objects.create(
            analysis=analysis,
            revision=analysis.revision,
            target=target,
            target_type=target_type,
            decision=decision,
            actor=user,
            reason=reason,
            idempotency_key=idempotency_key,
            fingerprint=request_hash,
        )
        if target_type == "clause":
            cls.compare(analysis, step)
        if target_type == "deviation" and obj.finding_id:
            obj.finding.status = (
                "dismissed"
                if decision == "dismissed"
                else "accepted"
                if decision in ("accepted", "exception_approved")
                else "open"
            )
            obj.finding.review_state = "reviewed"
            obj.finding.step_run = step
            obj.finding.save(
                update_fields=["status", "review_state", "step_run", "updated_at"]
            )
        WorkflowEngineService.set_state(run, run.status, user)
        workflow_event(
            run,
            user,
            "clause_review_decided",
            analysis_uuid=str(analysis.uuid),
            revision=analysis.revision,
            target_uuid=str(target),
            decision=decision,
        )
        notify_workflow(run, user, "clause_review_decided")
        return result

    @classmethod
    @transaction.atomic
    def correct(
        cls,
        user,
        analysis,
        *,
        clause,
        clause_type,
        facts,
        evidence,
        reason,
        expected_revision,
    ):
        run, analysis, step = cls.locked_review(user, analysis, expected_revision)
        cls.reason(reason)
        DocumentPermissionService.require_action(user, analysis.document, "correct")
        obj = analysis.clauses.filter(uuid=clause).first()
        if not obj:
            raise ValidationError("Clause correction target is unavailable.")
        record = {"clause_type": str(clause_type), "facts": facts, "evidence": evidence}
        ClauseDetectionService.validate_record(
            analysis.document, record, analysis.playbook_version.taxonomy
        )
        analysis.revision += 1
        result = ClauseCorrection.objects.create(
            clause=obj,
            revision=analysis.revision,
            clause_type_id=cls.type_id(str(clause_type)),
            facts=facts,
            evidence=evidence,
            actor=user,
            reason=reason,
        )
        analysis.save(update_fields=["revision", "updated_at"])
        cls.compare(analysis, step)
        WorkflowEngineService.set_state(run, run.status, user)
        workflow_event(
            run,
            user,
            "clause_corrected",
            analysis_uuid=str(analysis.uuid),
            clause_uuid=str(obj.uuid),
            revision=analysis.revision,
        )
        return result

    @classmethod
    def assert_review(cls, analysis):
        cls.authorize(analysis.workflow_run.initiated_by, analysis)
        if not analysis.coverage.get("complete"):
            raise ValidationError(
                "Complete source coverage is required before legal analysis finalization."
            )
        decisions = {}
        for decision in analysis.decisions.filter(revision=analysis.revision):
            decisions.setdefault(
                (decision.target_type, str(decision.target)), decision.decision
            )
        for row in cls.clauses(analysis):
            ClauseDetectionService.validate_record(
                analysis.document, row, analysis.playbook_version.taxonomy
            )
            if decisions.get(("clause", row["uuid"])) not in ("confirmed", "dismissed"):
                raise ValidationError(
                    "Review every current clause detection before accepting analysis."
                )
        for item in analysis.deviations.filter(revision=analysis.revision):
            if item.comparison not in SAFE_COMPARISONS and decisions.get(
                ("deviation", str(item.uuid))
            ) not in ("accepted", "dismissed", "exception_approved"):
                raise ValidationError(
                    "Resolve every current legal deviation before accepting analysis."
                )
            # Dismissal cannot turn an unsupported extraction into a preferred position.
            if item.comparison == "ambiguous":
                raise ValidationError(
                    "Correct ambiguous clause facts or context before accepting analysis."
                )

    @staticmethod
    def approval_required(analysis):
        return analysis.deviations.filter(
            revision=analysis.revision, requires_approval=True
        ).exists()

    @classmethod
    @transaction.atomic
    def finalize(cls, analysis):
        run = WorkflowEngineService.locked(analysis.workflow_run)
        analysis = ClauseAnalysis.objects.select_for_update().get(pk=analysis.pk)
        if run.status != "completed" and not (
            run.status == "processing"
            and run.current_step.key
            in ("finalize_clause_analysis", "finalize_contract_review")
        ):
            raise ValidationError(
                "Legal output requires the workflow finalization boundary."
            )
        cls.assert_review(analysis)
        approval = run.step_runs.filter(
            step__step_type="approval", approval__status="approved"
        ).first()
        if cls.approval_required(analysis) and not approval:
            raise ValidationError("The pinned playbook requires completed approval.")
        existing = analysis.outputs.filter(revision=analysis.revision).first()
        if existing:
            return existing.data
        # Normalize UUID/timestamps through Django's encoder before hashing immutable output.
        import json
        from django.core.serializers.json import DjangoJSONEncoder

        data = json.loads(json.dumps(cls.snapshot(analysis), cls=DjangoJSONEncoder))
        data["approval_uuid"] = str(approval.approval.uuid) if approval else None
        validation.bounded_json(data, 2097152)
        output = ClauseAnalysisOutput.objects.create(
            analysis=analysis,
            revision=analysis.revision,
            data=data,
            digest=fingerprint(data),
        )
        analysis.finalized_at = timezone.now()
        analysis.save(update_fields=["finalized_at", "updated_at"])
        workflow_event(
            run,
            run.initiated_by,
            "clause_output_finalized",
            analysis_uuid=str(analysis.uuid),
            revision=analysis.revision,
            digest=output.digest,
        )
        return data

    @classmethod
    @transaction.atomic
    def reanalyze(cls, user, analysis, *, version, idempotency_key):
        cls.authorize(user, analysis, source=False)
        WorkflowPermissionService.require(user, analysis.workflow_run, "administer")
        return WorkflowEngineService.start(
            user,
            version_uuid=version,
            document_uuids=[analysis.document.uuid],
            idempotency_key=idempotency_key,
            metadata={"previous_clause_analysis": str(analysis.uuid)},
        )[0]
