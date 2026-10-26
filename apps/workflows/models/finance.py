from django.conf import settings
from django.db import models

from apps.shared.models import BaseModel
from apps.workflows.models.engine import ImmutableVersionPart


class FinanceAudit(BaseModel):
    company = models.ForeignKey("tenants.Company", on_delete=models.PROTECT)
    department = models.ForeignKey(
        "workspaces.Department", null=True, on_delete=models.PROTECT
    )
    title = models.CharField(max_length=200)
    scope = models.JSONField()
    policy = models.JSONField()
    revision = models.PositiveIntegerField(default=1)
    population_revision = models.PositiveIntegerField(default=1)
    run = models.OneToOneField(
        "workflows.WorkflowRun", null=True, on_delete=models.PROTECT
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)

    class Meta:
        indexes = [models.Index(fields=["company", "department"])]


class FinanceDataset(ImmutableVersionPart):
    audit = models.ForeignKey(
        FinanceAudit, on_delete=models.PROTECT, related_name="datasets"
    )
    document = models.ForeignKey("documents.Document", on_delete=models.PROTECT)
    number = models.PositiveIntegerField()
    checksum = models.CharField(max_length=64)
    governance_revision = models.PositiveIntegerField()
    mapping = models.JSONField(default=dict)
    kind = models.CharField(max_length=20)
    row_count = models.PositiveIntegerField()
    totals = models.JSONField(default=dict)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    idempotency_key = models.CharField(max_length=128)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["audit", "idempotency_key"], name="finance_dataset_command"
            ),
            models.UniqueConstraint(
                fields=["audit", "number"], name="finance_dataset_version"
            ),
        ]


class FinanceTransaction(ImmutableVersionPart):
    dataset = models.ForeignKey(
        FinanceDataset, on_delete=models.PROTECT, related_name="transactions"
    )
    row = models.PositiveIntegerField()
    identifier = models.CharField(max_length=150)
    transaction_date = models.DateField()
    transaction_type = models.CharField(max_length=40)
    currency = models.CharField(max_length=3)
    amount = models.DecimalField(max_digits=30, decimal_places=8, null=True)
    vendor = models.CharField(max_length=150, blank=True)
    invoice_reference = models.CharField(max_length=150, blank=True)
    payment_reference = models.CharField(max_length=150, blank=True)
    data = models.JSONField()
    provenance = models.JSONField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["dataset", "row"], name="finance_source_row"
            )
        ]
        indexes = [
            models.Index(fields=["dataset", "transaction_date"]),
            models.Index(fields=["dataset", "vendor", "invoice_reference"]),
            models.Index(fields=["dataset", "payment_reference"]),
        ]


class FinanceCorrection(ImmutableVersionPart):
    transaction = models.ForeignKey(
        FinanceTransaction, on_delete=models.PROTECT, related_name="corrections"
    )
    number = models.PositiveIntegerField()
    original = models.JSONField()
    corrected = models.JSONField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    reason = models.CharField(max_length=1000)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["transaction", "number"], name="finance_correction_version"
            )
        ]


class FinanceEvidenceLink(ImmutableVersionPart):
    text_digest = models.CharField(max_length=64)
    file_digest = models.CharField(max_length=64, blank=True)
    audit = models.ForeignKey(
        FinanceAudit, on_delete=models.PROTECT, related_name="evidence_links"
    )
    transaction = models.ForeignKey(
        FinanceTransaction,
        null=True,
        on_delete=models.PROTECT,
        related_name="evidence_links",
    )
    document = models.ForeignKey("documents.Document", on_delete=models.PROTECT)
    role = models.CharField(max_length=30)
    checksum = models.CharField(max_length=64, blank=True)
    governance_revision = models.PositiveIntegerField()
    data = models.JSONField(default=dict)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["audit", "transaction", "document", "role"],
                name="finance_evidence_source",
            )
        ]


class FinanceSample(ImmutableVersionPart):
    audit = models.ForeignKey(
        FinanceAudit, on_delete=models.PROTECT, related_name="samples"
    )
    population_revision = models.PositiveIntegerField()
    population_digest = models.CharField(max_length=64)
    method = models.CharField(max_length=30)
    parameters = models.JSONField()
    selected = models.JSONField(default=list)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    idempotency_key = models.CharField(max_length=128)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["audit", "idempotency_key"], name="finance_sample_command"
            )
        ]


class FinanceTestRun(ImmutableVersionPart):
    audit = models.ForeignKey(
        FinanceAudit, on_delete=models.PROTECT, related_name="test_runs"
    )
    run = models.ForeignKey("workflows.WorkflowRun", on_delete=models.PROTECT)
    sample = models.ForeignKey(FinanceSample, null=True, on_delete=models.PROTECT)
    previous = models.ForeignKey("self", null=True, on_delete=models.PROTECT)
    population_revision = models.PositiveIntegerField()
    population_digest = models.CharField(max_length=64)
    definitions = models.JSONField()
    counts = models.JSONField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    started_at = models.DateTimeField()
    completed_at = models.DateTimeField()
    idempotency_key = models.CharField(max_length=128)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["audit", "idempotency_key"], name="finance_test_command"
            )
        ]


class FinanceTestResult(ImmutableVersionPart):
    test_run = models.ForeignKey(
        FinanceTestRun, on_delete=models.PROTECT, related_name="results"
    )
    transaction = models.ForeignKey(FinanceTransaction, on_delete=models.PROTECT)
    correction = models.ForeignKey(
        FinanceCorrection, null=True, on_delete=models.PROTECT
    )
    code = models.CharField(max_length=80)
    status = models.CharField(max_length=30)
    evidence = models.JSONField()
    finding = models.OneToOneField(
        "workflows.WorkflowFinding", null=True, on_delete=models.PROTECT
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["test_run", "transaction", "code"],
                name="finance_test_result_key",
            )
        ]


class FinanceManagementResponse(ImmutableVersionPart):
    audit = models.ForeignKey(
        FinanceAudit, on_delete=models.PROTECT, related_name="responses"
    )
    finding = models.ForeignKey("workflows.WorkflowFinding", on_delete=models.PROTECT)
    number = models.PositiveIntegerField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    data = models.JSONField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["finding", "number"], name="finance_response_version"
            )
        ]


class FinanceFindingDecision(ImmutableVersionPart):
    audit = models.ForeignKey(
        FinanceAudit, on_delete=models.PROTECT, related_name="decisions"
    )
    finding = models.ForeignKey("workflows.WorkflowFinding", on_delete=models.PROTECT)
    number = models.PositiveIntegerField()
    decision = models.CharField(max_length=30)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    reason = models.CharField(max_length=1000)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["finding", "number"], name="finance_decision_version"
            )
        ]


class FinanceAuditOutput(ImmutableVersionPart):
    audit = models.ForeignKey(
        FinanceAudit, on_delete=models.PROTECT, related_name="outputs"
    )
    run = models.OneToOneField("workflows.WorkflowRun", on_delete=models.PROTECT)
    number = models.PositiveIntegerField()
    data = models.JSONField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["audit", "number"], name="finance_output_version"
            )
        ]
