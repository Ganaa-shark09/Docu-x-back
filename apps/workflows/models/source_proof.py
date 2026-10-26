"""Additive provenance records; domain outputs remain authoritative."""

from django.conf import settings
from django.db import models

from apps.shared.models import BaseModel
from .compliance import ImmutableCompliancePart


class EvidenceSource(ImmutableCompliancePart):
    company = models.ForeignKey("tenants.Company", on_delete=models.PROTECT)
    kind = models.CharField(max_length=40)
    reference = models.UUIDField()
    identity = models.CharField(max_length=64)
    pin = models.JSONField()
    documents = models.ManyToManyField("documents.Document")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "kind", "reference", "identity"],
                name="proof_source_identity",
            )
        ]
        indexes = [models.Index(fields=["company", "kind", "reference"])]


class EvidenceRecord(ImmutableCompliancePart):
    source = models.ForeignKey(
        EvidenceSource, on_delete=models.PROTECT, related_name="records"
    )
    identity = models.CharField(max_length=64)
    locator_type = models.CharField(max_length=30)
    locator = models.JSONField()
    precision = models.CharField(max_length=30)
    method = models.CharField(max_length=40)
    disclosure = models.CharField(max_length=30, default="internal_only")
    valid_from = models.DateTimeField(null=True)
    valid_until = models.DateTimeField(null=True)
    review_due_at = models.DateTimeField(null=True)
    collected_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source", "identity"], name="proof_evidence_identity"
            )
        ]
        indexes = [models.Index(fields=["valid_until"])]


class EvidenceValidation(ImmutableCompliancePart):
    evidence = models.ForeignKey(
        EvidenceRecord, on_delete=models.PROTECT, related_name="validations"
    )
    number = models.PositiveIntegerField()
    status = models.CharField(max_length=30)
    code = models.CharField(max_length=40)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["evidence", "number"], name="proof_validation_number"
            )
        ]
        ordering = ["-number"]


class ProofClaim(BaseModel):
    company = models.ForeignKey("tenants.Company", on_delete=models.PROTECT)
    domain = models.CharField(max_length=40)
    origin = models.ForeignKey(EvidenceSource, on_delete=models.PROTECT)
    key = models.CharField(max_length=250)
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["origin", "key"], name="proof_claim_origin_key"
            )
        ]
        indexes = [models.Index(fields=["company", "domain"])]


class ClaimRevision(ImmutableCompliancePart):
    claim = models.ForeignKey(
        ProofClaim, on_delete=models.PROTECT, related_name="versions"
    )
    number = models.PositiveIntegerField()
    value = models.JSONField(null=True)
    status = models.CharField(max_length=30, default="unreviewed")
    previous = models.ForeignKey("self", null=True, on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    reason = models.CharField(max_length=4000, blank=True)
    command_key = models.CharField(max_length=128)
    command_digest = models.CharField(max_length=64)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["claim", "number"], name="proof_claim_revision"
            ),
            models.UniqueConstraint(
                fields=["claim", "command_key"], name="proof_claim_command"
            ),
        ]
        ordering = ["number"]


class ClaimEvidenceLink(ImmutableCompliancePart):
    version = models.ForeignKey(
        ClaimRevision, on_delete=models.PROTECT, related_name="evidence_links"
    )
    evidence = models.ForeignKey(EvidenceRecord, on_delete=models.PROTECT)
    role = models.CharField(max_length=30)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["version", "evidence", "role"], name="proof_claim_evidence_role"
            )
        ]


class ProofDerivation(ImmutableCompliancePart):
    result = models.OneToOneField(
        ClaimRevision, on_delete=models.PROTECT, related_name="derivation"
    )
    operation = models.CharField(max_length=40)
    parameters = models.JSONField(default=dict)
    inputs = models.ManyToManyField(ClaimRevision, related_name="derived_results")


class ProofReview(ImmutableCompliancePart):
    evidence = models.ForeignKey(
        EvidenceRecord, on_delete=models.PROTECT, related_name="reviews"
    )
    number = models.PositiveIntegerField()
    decision = models.CharField(max_length=30)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    reason = models.CharField(max_length=2000)
    command_key = models.CharField(max_length=128)
    command_digest = models.CharField(max_length=64)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["evidence", "number"], name="proof_review_number"
            ),
            models.UniqueConstraint(
                fields=["evidence", "command_key"], name="proof_review_command"
            ),
        ]
        ordering = ["-number"]
