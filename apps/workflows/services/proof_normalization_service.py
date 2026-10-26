"""Progressive, idempotent domain adapters; no destructive historical backfill."""

import json
from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.ai_engine.models import AIMessage
from apps.ai_engine.services.message_visibility_service import MessageVisibilityService
from apps.workflows.models.source_proof import (
    ProofDerivation,
    ClaimEvidenceLink,
    ClaimRevision,
)
from apps.workflows.services.finance_audit_service import digest
from .proof_source_service import ProofSourceService, output_path
from .source_proof_service import SourceProofService as proof, event


class ProofNormalizationService:
    @classmethod
    def location(cls, user, kind, source_uuid, item, resolver, **validity):
        """Only native stored locator fields are mapped; no guessed offsets."""
        if not isinstance(item, dict):
            raise ValidationError("Legacy evidence requires a structured reference.")
        locator = {"document_uuid": item.get("document_uuid")}
        if type(item.get("start")) is int and type(item.get("end")) is int:
            resolved = resolver.resolve(kind, source_uuid)
            doc = next(
                (
                    d
                    for d in resolved["documents"]
                    if str(d.uuid) == item.get("document_uuid")
                ),
                None,
            )
            if not doc:
                raise ValidationError("Evidence locator is unavailable.")
            text = resolver.document(doc)[1]
            quote = item.get("quote", item.get("excerpt"))
            preview = item.get("preview")
            if quote is not None and quote != text[item["start"] : item["end"]]:
                raise ValidationError({"code": "excerpt_mismatch"})
            if (
                preview is not None
                and preview != text[item["start"] : item["end"]][:500]
            ):
                raise ValidationError({"code": "excerpt_mismatch"})
            locator.update(start=item["start"], end=item["end"])
            if item.get("page") is not None:
                locator["page"] = item["page"]
            return proof.capture(
                user,
                kind,
                source_uuid,
                "text_offsets",
                locator,
                "exact",
                method="imported_data",
                resolver=resolver,
                **validity,
            )
        if item.get("chunk_uuid"):
            locator["chunk_uuid"] = item["chunk_uuid"]
            return proof.capture(
                user,
                kind,
                source_uuid,
                "chunk",
                locator,
                "approximate",
                excerpt=item.get("quote"),
                method="retrieval",
                resolver=resolver,
                **validity,
            )
        return proof.capture(user, kind, source_uuid, resolver=resolver, **validity)

    @classmethod
    @transaction.atomic
    def output(cls, user, kind, source_uuid, path, resolver=None):
        """One explicitly requested output field; bounds prevent proof amplification.

        Domain data is not reinterpreted: facts, policy, deviation, risk and
        acceptance remain separate named paths in the immutable source output.
        """
        resolver = resolver or ProofSourceService(user)
        resolved = resolver.resolve(kind, source_uuid)
        value = output_path(resolved["data"], path)
        row = proof.capture(
            user,
            kind,
            source_uuid,
            "output_path",
            {"path": path},
            "structured_record",
            method="workflow_rule",
            resolver=resolver,
        )
        claim = proof.claim(user, row, json.dumps(path, separators=(",", ":")), value)
        version = claim.versions.get(number=1)
        operation = "workflow_aggregation"
        inputs = []
        if (
            kind in {"invoice_output", "contract_output"}
            and len(path) >= 2
            and path[0] in {"invoice", "contract", "field_provenance"}
        ):
            key = path[1]
            provenance = resolved["data"].get("field_provenance", {}).get(key, {})
            for item in provenance.get("evidence", [])[:20]:
                evidence = cls.location(user, kind, source_uuid, item, resolver)
                ClaimEvidenceLink.objects.get_or_create(
                    version=version, evidence=evidence, role="supports"
                )
            operation = "normalization"
            if provenance.get("field_uuid"):
                from apps.workflows.models import ExtractionResultField

                field = ExtractionResultField.objects.filter(
                    uuid=provenance["field_uuid"],
                    extraction_run__company_id=user.company_id,
                ).first()
                if field and field.is_corrected:
                    original = proof.claim(
                        user,
                        row,
                        "original:" + str(field.uuid),
                        field.extracted_value,
                        role="context",
                        domain="extraction_original",
                    )
                    old = original.versions.get(number=1)
                    corrections = list(field.corrections.order_by("revision")[:21])
                    if len(corrections) > 20:
                        raise ValidationError(
                            "Correction history exceeds bounded proof scope."
                        )
                    for correction in corrections:
                        imported, _ = ClaimRevision.objects.get_or_create(
                            claim=original,
                            command_key=str(correction.uuid),
                            defaults={
                                "number": old.number + 1,
                                "value": correction.corrected_value,
                                "status": "corrected",
                                "previous": old,
                                "actor": correction.actor or user,
                                "reason": correction.reason,
                                "command_digest": digest(
                                    [str(correction.uuid), correction.corrected_value]
                                ),
                            },
                        )
                        ClaimEvidenceLink.objects.get_or_create(
                            version=imported, evidence=row, role="context"
                        )
                        step, _ = ProofDerivation.objects.get_or_create(
                            result=imported,
                            defaults={
                                "operation": "human_correction",
                                "parameters": {
                                    "correction_uuid": str(correction.uuid),
                                    "occurred_at": correction.created_at.isoformat(),
                                },
                            },
                        )
                        step.inputs.add(old)
                        old = imported
                    original.revision = old.number
                    original.save(update_fields=["revision", "updated_at"])
                    inputs.append(old)
        elif kind == "hr_policy_version":
            for section in resolved["object"].sections.all()[:20]:
                evidence = cls.location(
                    user, kind, source_uuid, section.evidence, resolver
                )
                ClaimEvidenceLink.objects.get_or_create(
                    version=version, evidence=evidence, role="context"
                )
        elif kind == "finance_transaction":
            evidence = proof.capture(
                user,
                kind,
                source_uuid,
                "transaction",
                {"transaction_uuid": str(resolved["object"].uuid)},
                "structured_record",
                resolver=resolver,
            )
            ClaimEvidenceLink.objects.get_or_create(
                version=version, evidence=evidence, role="calculation_input"
            )
            operation = "imported_data"
        elif kind == "rfp_response":
            for item in resolved["object"].evidence[:20]:
                evidence = cls.location(user, kind, source_uuid, item, resolver)
                ClaimEvidenceLink.objects.get_or_create(
                    version=version, evidence=evidence, role="supports"
                )
        elif kind == "rfp_output" and path[0] == "responses":
            selected = resolved["data"]["responses"]
            if len(path) > 1 and type(path[1]) is int:
                selected = [selected[path[1]]]
            if len(selected) > 20:
                raise ValidationError(
                    "Request proof for a single response or bounded section."
                )
            for item in selected:
                if item.get("response_uuid"):
                    response_claim = cls.output(
                        user,
                        "rfp_response",
                        item["response_uuid"],
                        ["text"],
                        resolver=resolver,
                    )
                    inputs.append(response_claim.versions.get(number=1))
        elif kind == "clause_output" and path[0] in {"clauses", "deviations"}:
            selected = resolved["data"][path[0]]
            if len(path) > 1 and type(path[1]) is int:
                selected = [selected[path[1]]]
            if sum(len(item.get("evidence", [])) for item in selected) > 20:
                raise ValidationError(
                    "Request proof for a bounded clause or deviation."
                )
            for item in selected:
                for native in item.get("evidence", []):
                    evidence = cls.location(user, kind, source_uuid, native, resolver)
                    ClaimEvidenceLink.objects.get_or_create(
                        version=version,
                        evidence=evidence,
                        role="supports" if path[0] == "clauses" else "context",
                    )
        elif kind == "compliance_output" and path[0] in {"rows", "evidence"}:
            selected = resolved["data"]["rows"]
            if path[0] == "rows" and len(path) > 1 and type(path[1]) is int:
                selected = [selected[path[1]]]
            identifiers = {
                identifier for r in selected for identifier in r.get("evidence", [])
            }
            native_links = list(
                resolved["object"].assessment.evidence.filter(uuid__in=identifiers)[:21]
            )
            if len(native_links) > 20:
                raise ValidationError(
                    "Request proof for one control or bounded requirement."
                )
            for native in native_links:
                dates = {}
                for source_key, target_key in [
                    ("effective", "valid_from"),
                    ("expires", "valid_until"),
                    ("review_due", "review_due_at"),
                ]:
                    if native.dates.get(source_key):
                        dates[target_key] = timezone.make_aware(
                            datetime.fromisoformat(native.dates[source_key])
                        )
                        if source_key == "review_due":
                            # Native date-only due dates are inclusive of that day.
                            dates[target_key] += timedelta(days=1)
                location = (
                    {"document_uuid": str(native.source_uuid), **native.location}
                    if native.source_kind == "document"
                    else {}
                )
                evidence = cls.location(
                    user,
                    native.source_kind,
                    native.source_uuid,
                    location,
                    resolver,
                    **dates,
                )
                ClaimEvidenceLink.objects.get_or_create(
                    version=version, evidence=evidence, role="supports"
                )
        derivation, _ = ProofDerivation.objects.get_or_create(
            result=version, defaults={"operation": operation}
        )
        derivation.inputs.add(*inputs)
        proof.graph(resolver, claim)
        event(user, claim, "proof_generated")
        return claim

    @classmethod
    @transaction.atomic
    def message(cls, user, message_uuid):
        message = (
            AIMessage.objects.filter(
                uuid=message_uuid,
                conversation__company_id=user.company_id,
                conversation__user=user,
                conversation__scope="internal",
                role="assistant",
                is_deleted=False,
            )
            .select_related("conversation")
            .first()
        )
        if not message or not MessageVisibilityService.visible(message, user):
            raise PermissionDenied("Answer source proof is unavailable.")
        # Conversation summaries and source-less prior answers are context only.
        if (
            (message.metadata or {}).get("intent") == "conversation_summary"
            or not message.retrieved_sources
            or len(message.retrieved_sources) > 20
        ):
            raise ValidationError({"code": "legacy_unverified"})
        pins = (message.metadata or {}).get("source_proof_pins", {})
        resolver = ProofSourceService(user, "ai")
        rows = []
        for item in message.retrieved_sources:
            doc_uuid = item.get("document_uuid")
            resolved = resolver.resolve("document", doc_uuid)
            # Never backfill today's version as if an old answer cited it.
            if pins.get(str(doc_uuid)) != resolved["pin"]:
                raise PermissionDenied(
                    "Answer source lineage is unavailable or changed."
                )
            kind = (
                "hr_policy_version" if item.get("policy_version_uuid") else "document"
            )
            source_uuid = item.get("policy_version_uuid") or doc_uuid
            rows.append(cls.location(user, kind, source_uuid, item, resolver))
        # No automatic semantic claim extraction: a clearly labelled answer-level
        # association retains each original citation without asserting entailment.
        claim = proof.claim(
            user,
            rows[0],
            "ai_answer:" + str(message.uuid),
            message.content,
            role="context",
            domain="internal_ai_answer",
        )
        version = claim.versions.get(number=1)
        for row in rows[1:]:
            ClaimEvidenceLink.objects.get_or_create(
                version=version, evidence=row, role="context"
            )
        ProofDerivation.objects.get_or_create(
            result=version,
            defaults={
                "operation": "retrieval",
                "parameters": {
                    "message_uuid": str(message.uuid),
                    "citation_identity": digest(message.retrieved_sources),
                },
            },
        )
        proof.graph(resolver, claim)
        return claim
