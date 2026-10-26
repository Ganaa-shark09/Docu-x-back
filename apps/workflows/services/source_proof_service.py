"""Permission checked provenance graphs. Proof is traceability, not truth."""

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.audit.services import AuditLogService
from apps.tenants.models import Company
from apps.workflows.models.source_proof import (
    EvidenceSource,
    EvidenceRecord,
    EvidenceValidation,
    ProofClaim,
    ClaimRevision,
    ClaimEvidenceLink,
    ProofDerivation,
    ProofReview,
)
from apps.workflows.services.finance_audit_service import digest
from apps.workflows.services.workflow_definition_service import (
    bounded_json,
    WorkflowDefinitionService,
)
from apps.workflows.services.workflow_engine_service import WorkflowConflict
from .proof_source_service import ProofSourceService
from .proof_locator_service import ProofLocatorService


def event(user, row, action):
    AuditLogService.log(
        company=user.company,
        user=user,
        event_type="workflow_event",
        action="Source proof " + action,
        resource_type="source_proof",
        resource_uuid=row.uuid,
        metadata={"event": action},
    )


class SourceProofService:
    MAX_NODES = 200
    MAX_DEPTH = 8

    @staticmethod
    def fresh(user):
        return ProofSourceService(user)

    @staticmethod
    def source(resolver, source):
        if source.company_id != resolver.user.company_id or source.is_deleted:
            raise PermissionDenied("Proof is unavailable.")
        result = resolver.resolve(source.kind, source.reference)
        return result, result["pin"] == source.pin

    @classmethod
    def capture(
        cls,
        user,
        kind,
        source_uuid,
        locator_type="source",
        locator=None,
        precision="approximate",
        excerpt=None,
        method="imported_data",
        disclosure="internal_only",
        valid_from=None,
        valid_until=None,
        review_due_at=None,
        resolver=None,
    ):
        resolver = resolver or cls.fresh(user)
        if method not in {
            "deterministic_parser",
            "ocr",
            "structured_extraction",
            "llm_extraction",
            "user_correction",
            "imported_data",
            "workflow_rule",
            "calculation",
            "retrieval",
        } or disclosure not in {
            "internal_only",
            "exportable",
            "redacted_export",
            "restricted",
        }:
            raise ValidationError("Invalid evidence method or disclosure.")
        if valid_from and valid_until and valid_from >= valid_until:
            raise ValidationError("Invalid evidence validity interval.")
        resolved = resolver.resolve(kind, source_uuid)
        clean = ProofLocatorService.validate(
            resolver,
            resolved,
            locator_type,
            {} if locator is None else locator,
            precision,
            excerpt,
        )
        bounded_json(clean)
        pin = resolved["pin"]
        identity = digest(
            {
                "locator_type": locator_type,
                "locator": clean,
                "precision": precision,
                "method": method,
                "disclosure": disclosure,
                "dates": [
                    d.isoformat() if d else None
                    for d in (valid_from, valid_until, review_due_at)
                ],
            }
        )
        with transaction.atomic():
            # Narrow, deterministic serialization point, shared with domain commands.
            Company.objects.select_for_update().get(pk=user.company_id)
            source, created = EvidenceSource.objects.get_or_create(
                company=user.company,
                kind=kind,
                reference=pin["reference"],
                identity=digest(pin),
                defaults={"pin": pin},
            )
            if created:
                source.documents.set(resolved["documents"])
            row, created = EvidenceRecord.objects.get_or_create(
                source=source,
                identity=identity,
                defaults={
                    "locator_type": locator_type,
                    "locator": clean,
                    "precision": precision,
                    "method": method,
                    "disclosure": disclosure,
                    "valid_from": valid_from,
                    "valid_until": valid_until,
                    "review_due_at": review_due_at,
                    "collected_by": user,
                },
            )
            if created:
                event(user, row, "evidence_created")
            return row

    @classmethod
    def validity(cls, resolver, row):
        resolved, matches = cls.source(resolver, row.source)
        if not matches:
            return "stale", "source_changed"
        if not resolved["pin"].get("lineage_verified", False):
            return "insufficient", "legacy_unverified"
        try:
            ProofLocatorService.revalidate(resolver, resolved, row)
        except ValidationError:
            return "invalid", "locator_invalid"
        now = timezone.now()
        if row.valid_until and row.valid_until <= now:
            return "expired", "evidence_expired"
        if not resolved["current"] or (row.review_due_at and row.review_due_at <= now):
            return "stale", "source_superseded"
        if row.valid_from and row.valid_from > now:
            return "insufficient", "not_yet_effective"
        review = row.reviews.first()
        if review and review.decision == "rejected":
            return "invalid", "review_rejected"
        if row.locator_type == "source":
            return "insufficient", "legacy_unverified"
        return "valid", "validated_source"

    @classmethod
    def evidence_data(cls, resolver, row):
        status, code = cls.validity(resolver, row)
        review = row.reviews.first()
        return {
            "uuid": str(row.uuid),
            "source": {
                "uuid": str(row.source.uuid),
                "kind": row.source.kind,
                "reference": str(row.source.reference),
                "identity": row.source.identity,
                "pin": row.source.pin,
            },
            "locator_type": row.locator_type,
            "locator": row.locator,
            "precision": row.precision,
            "method": row.method,
            "disclosure": row.disclosure,
            "validity": status,
            "code": code,
            "captured_at": row.created_at.isoformat(),
            "review_state": review.decision if review else "unreviewed",
            "review_revision": review.number if review else 0,
            "valid_from": row.valid_from,
            "valid_until": row.valid_until,
            "review_due_at": row.review_due_at,
        }

    @classmethod
    @transaction.atomic
    def validate(cls, user, row):
        resolver = cls.fresh(user)
        row = (
            EvidenceRecord.objects.select_for_update()
            .select_related("source")
            .get(pk=row.pk)
        )
        # A validation history contains no source content; inaccessible sources
        # are recorded as revoked and the API still refuses their disclosure.
        if row.source.company_id != user.company_id:
            raise PermissionDenied("Proof is unavailable.")
        try:
            status, code = cls.validity(resolver, row)
        except PermissionDenied:
            status, code = "revoked", "source_unavailable"
        previous = row.validations.first()
        if not previous or (previous.status, previous.code) != (status, code):
            previous = EvidenceValidation.objects.create(
                evidence=row,
                number=previous.number + 1 if previous else 1,
                status=status,
                code=code,
                actor=user,
            )
            event(user, row, "evidence_" + status)
        return previous

    @classmethod
    def require_editor(cls, user, source):
        resolver = cls.fresh(user)
        resolved, _ = cls.source(resolver, source)
        # Canonical review never changes a domain workflow's decision/output.
        for doc in resolved["documents"]:
            WorkflowDefinitionService.require_admin(user, doc.department_id)
        return resolver

    @classmethod
    @transaction.atomic
    def review(cls, user, row, revision, decision, reason, idempotency_key):
        resolver = cls.require_editor(user, row.source)
        row = EvidenceRecord.objects.select_for_update().get(pk=row.pk)
        state, _ = cls.validity(resolver, row)
        if (
            decision not in {"confirmed", "rejected"}
            or not isinstance(reason, str)
            or not 1 <= len(reason.strip()) <= 2000
            or not 1 <= len(idempotency_key) <= 128
        ):
            raise ValidationError("Invalid proof review.")
        payload = digest([revision, decision, reason])
        old = row.reviews.filter(command_key=idempotency_key).first()
        if old:
            if old.command_digest != payload:
                raise WorkflowConflict("Idempotency key was reused.")
            return old
        if decision == "confirmed" and state != "valid":
            raise ValidationError("Only currently valid evidence can be confirmed.")
        latest = row.reviews.first()
        if revision != (latest.number if latest else 0):
            raise WorkflowConflict()
        result = ProofReview.objects.create(
            evidence=row,
            number=revision + 1,
            decision=decision,
            reason=reason,
            actor=user,
            command_key=idempotency_key,
            command_digest=payload,
        )
        event(user, row, "review_decided")
        return result

    @classmethod
    def claim(cls, user, evidence, key, value, role="source_of_value", domain=None):
        cls.validity(cls.fresh(user), evidence)
        if (
            role
            not in {
                "supports",
                "context",
                "calculation_input",
                "qualifies",
                "source_of_value",
                "contradicts_candidate",
            }
            or not isinstance(key, str)
            or not 1 <= len(key) <= 250
        ):
            raise ValidationError("Invalid claim relationship.")
        bounded_json(value)
        with transaction.atomic():
            Company.objects.select_for_update().get(pk=user.company_id)
            row, created = ProofClaim.objects.get_or_create(
                company=user.company,
                origin=evidence.source,
                key=key,
                defaults={"domain": domain or evidence.source.kind},
            )
            if created:
                ClaimRevision.objects.create(
                    claim=row,
                    number=1,
                    value=value,
                    actor=user,
                    command_key="origin",
                    command_digest=digest(value),
                )
            version = row.versions.get(number=1)
            if version.value != value:
                raise WorkflowConflict(
                    "Claim identity already refers to a different value."
                )
            ClaimEvidenceLink.objects.get_or_create(
                version=version, evidence=evidence, role=role
            )
            return row

    @classmethod
    def graph(cls, resolver, claim, external=False):
        claim = ProofClaim.objects.select_related("origin").get(pk=claim.pk)
        if claim.company_id != resolver.user.company_id or claim.is_deleted:
            raise PermissionDenied("Proof is unavailable.")
        nodes, evidence, visiting = {}, {}, set()

        def visit(version, depth):
            key = str(version.uuid)
            if (
                key in visiting
                or depth > cls.MAX_DEPTH
                or len(nodes) + len(evidence) >= cls.MAX_NODES
            ):
                raise ValidationError("Proof graph exceeds bounds or contains a cycle.")
            if key in nodes:
                return
            if version.claim.company_id != resolver.user.company_id:
                raise PermissionDenied("Proof is unavailable.")
            if version.claim.domain == "internal_ai_answer":
                from apps.ai_engine.models import AIMessage
                from apps.ai_engine.services.message_visibility_service import (
                    MessageVisibilityService,
                )

                message_uuid = version.claim.key.removeprefix("ai_answer:")
                message = (
                    AIMessage.objects.filter(
                        uuid=message_uuid,
                        conversation__user=resolver.user,
                        conversation__company_id=resolver.user.company_id,
                        is_deleted=False,
                    )
                    .select_related("conversation")
                    .first()
                )
                if not message or not MessageVisibilityService.visible(
                    message, resolver.user
                ):
                    raise PermissionDenied("Answer proof is unavailable.")
            resolved, matches = cls.source(resolver, version.claim.origin)
            if not matches:
                # No historical values are disclosed when the origin changes.
                raise PermissionDenied("Pinned proof origin changed.")
            if external and any(
                d.sensitivity_label != "public" for d in resolved["documents"]
            ):
                raise PermissionDenied("Proof disclosure is restricted.")
            if external and version.claim.origin.kind in {"rfp_output", "rfp_response"}:
                from apps.workflows.services.rfp_service import RFPService

                source_obj = resolved["object"]
                if version.claim.origin.kind == "rfp_response":
                    from apps.workflows.models import RFPProposalOutput

                    source_obj = RFPProposalOutput.objects.get(
                        uuid=resolved["pin"]["version"]["output_uuid"]
                    )
                RFPService.export(
                    resolver.user, source_obj.project, source_obj, external=True
                )
            if external and version.claim.origin.kind == "compliance_output":
                from apps.workflows.services.compliance_assessment_service import (
                    ComplianceAssessmentService,
                )

                ComplianceAssessmentService.export(
                    resolver.user,
                    resolved["object"].assessment,
                    resolved["object"],
                    external=True,
                )
            visiting.add(key)
            links = list(
                version.evidence_links.select_related(
                    "evidence__source"
                ).prefetch_related("evidence__reviews")[: cls.MAX_NODES + 1]
            )
            if len(links) > cls.MAX_NODES:
                raise ValidationError("Proof evidence scope exceeds bounds.")
            if not links:
                raise PermissionDenied("Proof lineage is incomplete.")
            refs = []
            for link in links:
                row = link.evidence
                if external and row.disclosure != "exportable":
                    raise PermissionDenied("Proof disclosure is restricted.")
                eid = str(row.uuid)
                if eid not in evidence:
                    if len(nodes) + len(evidence) + 2 > cls.MAX_NODES:
                        raise ValidationError("Proof graph exceeds bounds.")
                    evidence[eid] = cls.evidence_data(resolver, row)
                refs.append({"evidence_uuid": eid, "role": link.role})
            node = {
                "uuid": key,
                "claim_uuid": str(version.claim.uuid),
                "domain": version.claim.domain,
                "key": version.claim.key,
                "revision": version.number,
                "value": version.value,
                "review_state": version.status,
                "actor": None if external else version.actor_id,
                "reason": None if external else version.reason,
                "created_at": version.created_at.isoformat(),
                "evidence": refs,
                "previous_uuid": str(version.previous.uuid)
                if version.previous_id
                else None,
                "derivation": None,
            }
            nodes[key] = node
            derivation = getattr(version, "derivation", None)
            if derivation:
                inputs = list(
                    derivation.inputs.select_related("claim__origin")[
                        : cls.MAX_NODES + 1
                    ]
                )
                node["derivation"] = {
                    "operation": derivation.operation,
                    "parameters": derivation.parameters,
                    "inputs": [str(v.uuid) for v in inputs],
                }
                for item in inputs:
                    visit(item, depth + 1)
            if version.previous_id:
                visit(version.previous, depth + 1)
            visiting.remove(key)

        for version in claim.versions.filter(number__lte=claim.revision).select_related(
            "claim__origin", "previous"
        )[: cls.MAX_NODES + 1]:
            visit(version, 0)
        statuses = {r["validity"] for r in evidence.values()}
        coverage = (
            "fully_supported"
            if statuses == {"valid"}
            else "partially_supported"
            if "valid" in statuses
            else "unsupported"
        )
        return {
            "claim_uuid": str(claim.uuid),
            "current_revision": claim.revision,
            "coverage": coverage,
            "claims": list(nodes.values()),
            "evidence": list(evidence.values()),
            "purpose": "traceability",
        }

    @classmethod
    @transaction.atomic
    def correct(
        cls, user, claim, revision, value, reason, idempotency_key, evidence_uuids=None
    ):
        resolver = cls.require_editor(user, claim.origin)
        claim = ProofClaim.objects.select_for_update().get(pk=claim.pk)
        cls.graph(resolver, claim)
        bounded_json(value)
        if (
            not isinstance(reason, str)
            or not 1 <= len(reason.strip()) <= 2000
            or not 1 <= len(idempotency_key) <= 128
        ):
            raise ValidationError("Correction reason and key are required.")
        evidence_uuids = [str(v) for v in evidence_uuids] if evidence_uuids else None
        payload = digest([revision, value, reason, evidence_uuids])
        previous = claim.versions.filter(command_key=idempotency_key).first()
        if previous:
            if previous.command_digest != payload:
                raise WorkflowConflict()
            return claim
        if claim.revision != revision:
            raise WorkflowConflict()
        old = claim.versions.get(number=revision)
        links = list(old.evidence_links.select_related("evidence__source"))
        additional = []
        if evidence_uuids:
            if len(evidence_uuids) > 20:
                raise ValidationError("Too many correction sources.")
            additional = list(
                EvidenceRecord.objects.filter(
                    uuid__in=evidence_uuids, source__company_id=user.company_id
                ).select_related("source")
            )
            if len(additional) != len(set(evidence_uuids)):
                raise PermissionDenied("Correction evidence is unavailable.")
            for row in additional:
                cls.validity(resolver, row)
        new = ClaimRevision.objects.create(
            claim=claim,
            number=revision + 1,
            value=value,
            status="corrected",
            previous=old,
            actor=user,
            reason=reason,
            command_key=idempotency_key,
            command_digest=payload,
        )
        for link in links:
            ClaimEvidenceLink.objects.create(
                version=new, evidence=link.evidence, role=link.role
            )
        for row in additional:
            ClaimEvidenceLink.objects.get_or_create(
                version=new, evidence=row, role="source_of_value"
            )
        step = ProofDerivation.objects.create(result=new, operation="human_correction")
        step.inputs.add(old)
        claim.revision += 1
        claim.save(update_fields=["revision", "updated_at"])
        cls.graph(resolver, claim)
        event(user, claim, "claim_corrected")
        return claim

    @classmethod
    def export(cls, user, claim, external=False):
        result = cls.graph(ProofSourceService(user, "export"), claim, external)
        event(user, claim, "proof_exported")
        return result

    @classmethod
    @transaction.atomic
    def derive(cls, user, evidence, key, operation, inputs):
        """New nodes reference existing immutable revisions: cycles cannot be added.

        Initial arithmetic deliberately shares Finance's decimal representation;
        no float coercion, expressions, SQL, prompts or hidden reasoning.
        """
        from decimal import Decimal, InvalidOperation, localcontext

        resolver = cls.require_editor(user, evidence.source)
        if (
            operation not in {"sum", "subtract", "count"}
            or not 1 <= len(inputs) <= 20
            or (operation == "subtract" and len(inputs) != 2)
        ):
            raise ValidationError("Unsupported bounded derivation.")
        for item in inputs:
            cls.graph(resolver, item.claim)
        try:
            if operation == "count":
                value = str(len(inputs))
            else:
                values = []
                for item in inputs:
                    if not isinstance(item.value, str) or len(item.value) > 64:
                        raise ValueError
                    number = Decimal(item.value)
                    if (
                        not number.is_finite()
                        or abs(number) >= Decimal("1e30")
                        or number.as_tuple().exponent < -8
                    ):
                        raise ValueError
                    values.append(number)
                with localcontext() as context:
                    context.prec = 64
                    value = format(
                        sum(values, Decimal(0))
                        if operation == "sum"
                        else values[0] - values[1],
                        "f",
                    )
        except (InvalidOperation, ValueError):
            raise ValidationError("Calculation requires bounded exact Decimal inputs.")
        cls.validity(resolver, evidence)
        Company.objects.select_for_update().get(pk=user.company_id)
        result = cls.claim(
            user, evidence, key, value, "calculation_input", domain="calculation"
        )
        version = result.versions.get(number=1)
        # A result may never reuse an existing input's stable claim identity.
        if any(v.claim_id == result.pk for v in inputs):
            raise ValidationError("Cyclic derivation is not permitted.")
        parameters = {
            "operation": operation,
            "input_revisions": [str(v.uuid) for v in inputs],
        }
        previous = getattr(version, "derivation", None)
        if previous:
            if previous.parameters != parameters:
                raise WorkflowConflict("Derivation identity was reused.")
        else:
            step = ProofDerivation.objects.create(
                result=version, operation="calculation", parameters=parameters
            )
            step.inputs.set(inputs)
        cls.graph(resolver, result)
        return result
