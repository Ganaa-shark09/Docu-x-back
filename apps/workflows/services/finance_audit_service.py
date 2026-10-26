import hashlib
import heapq
import json
import re
from itertools import islice
from collections import Counter, defaultdict
from decimal import Decimal, localcontext
from functools import wraps

from django.db import transaction
from django.db.models import Max, Prefetch
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.audit.services import AuditLogService
from apps.documents.models import Document
from apps.documents.services import DocumentPermissionService
from apps.documents.services.field_permission_service import FieldPermissionService
from apps.workflows.models.finance import (
    FinanceAudit,
    FinanceDataset,
    FinanceTransaction,
    FinanceCorrection,
    FinanceEvidenceLink,
    FinanceSample,
    FinanceTestRun,
    FinanceTestResult,
    FinanceManagementResponse,
    FinanceFindingDecision,
    FinanceAuditOutput,
)
from apps.workflows.models import WorkflowFinding, WorkflowRunDocument
from apps.workflows.permissions.workflow_permissions import WorkflowPermissionService
from apps.workflows.services import finance_schema as schema
from apps.workflows.services.invoice_matching_service import (
    accessible_invoice_records,
    InvoiceMatchingService,
)
from apps.workflows.services.workflow_definition_service import (
    WorkflowDefinitionService,
    bounded_json,
)
from apps.workflows.services.workflow_engine_service import (
    WorkflowEngineService,
    WorkflowConflict,
)
from apps.workflows.services.workflow_events import notify_workflow


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def event(user, audit, name):
    AuditLogService.log(
        company=audit.company,
        user=user,
        event_type="workflow_event",
        action="Finance audit " + name,
        resource_type="finance_audit",
        resource_uuid=audit.uuid,
        metadata={"event": name, "revision": audit.revision},
    )


def number(query):
    return (query.aggregate(value=Max("number"))["value"] or 0) + 1


def exact_finance(function):
    @wraps(function)
    def call(*args, **kwargs):
        with localcontext() as context:
            context.prec = 64
            return function(*args, **kwargs)

    return call


def text_digest(document):
    extraction = getattr(document, "text_extraction", None)
    return digest(extraction.extracted_text if extraction else "")


def journal_lines(data):
    lines = data.get("lines") or []
    return (
        lines
        if any(
            line.get("debit") is not None or line.get("credit") is not None
            for line in lines
        )
        else [data]
    )


def total_value(data, key):
    if data.get(key) is not None:
        return Decimal(data[key])
    if key in {"debit", "credit"}:
        return sum(
            (Decimal(line.get(key) or "0") for line in journal_lines(data)), Decimal(0)
        )
    return None


class FinanceAuditService:
    @staticmethod
    def visible(user):
        if not WorkflowPermissionService.active(user):
            return FinanceAudit.objects.none()
        qs = FinanceAudit.objects.filter(
            company_id=user.company_id, is_deleted=False
        ).select_related("run", "company")
        if DocumentPermissionService.is_company_admin(user):
            return qs
        from django.db.models import Q

        return qs.filter(
            Q(department__isnull=True)
            | Q(
                department_id__in=DocumentPermissionService.get_user_department_ids(
                    user
                )
            )
        )

    @staticmethod
    def document(user, doc, action="view"):
        if (
            doc.company_id != user.company_id
            or doc.is_deleted
            or doc.scope != "internal"
            or (doc.retention_expires_at and doc.retention_expires_at <= timezone.now())
        ):
            raise PermissionDenied("Financial source is unavailable.")
        for permission in {"view", action}:
            DocumentPermissionService.require_action(user, doc, permission)
            if FieldPermissionService.has_restrictions(user, doc, permission):
                raise PermissionDenied("Field policy denies this financial derivative.")
        return doc

    @staticmethod
    def raw(doc):
        if not doc.file:
            raise ValidationError("A registered source file is required.")
        try:
            with doc.file.open("rb") as source:
                raw = source.read(8388609)
        except (OSError, ValueError):
            raise PermissionDenied("Financial source file is unavailable.")
        if len(raw) > 8388608:
            raise ValidationError("Financial source exceeds the bounded import limit.")
        return raw

    @classmethod
    def authorize(cls, user, audit, action="view"):
        def source_hash(doc):
            try:
                return hashlib.sha256(cls.raw(doc)).hexdigest()
            except ValidationError:
                raise PermissionDenied("Financial source file is unavailable.")

        if not cls.visible(user).filter(
            pk=audit.pk
        ).exists() or not WorkflowPermissionService.department_allowed(
            user, audit.department_id
        ):
            raise PermissionDenied("Finance audit is unavailable.")
        for dataset in audit.datasets.select_related("document"):
            doc = cls.document(user, dataset.document, action)
            if (
                doc.governance_revision != dataset.governance_revision
                or source_hash(doc) != dataset.checksum
            ):
                raise PermissionDenied(
                    "Financial dataset source changed or was revoked."
                )
        for link in audit.evidence_links.select_related("document"):
            doc = cls.document(user, link.document, action)
            if (
                doc.governance_revision != link.governance_revision
                or doc.checksum != link.checksum
            ):
                raise PermissionDenied("Supporting evidence changed or was revoked.")
            if text_digest(doc) != link.text_digest or (
                link.file_digest and source_hash(doc) != link.file_digest
            ):
                raise PermissionDenied("Supporting source content changed.")
            if link.role == "invoice":
                invoice = (
                    accessible_invoice_records(user)
                    .filter(uuid=link.data["invoice_uuid"])
                    .first()
                )
                if (
                    not invoice
                    or digest(invoice.normalized) != link.data["invoice_digest"]
                    or invoice.revision != link.data["invoice_revision"]
                ):
                    raise PermissionDenied("Matched invoice evidence is stale.")
        return audit

    @classmethod
    def locked(cls, user, audit, revision=None):
        from apps.tenants.models import Company

        snapshot = FinanceAudit.objects.get(pk=audit.pk)
        if snapshot.run_id:
            WorkflowEngineService.locked(snapshot.run)
        Company.objects.select_for_update().get(pk=snapshot.company_id)
        audit = FinanceAudit.objects.select_for_update().get(pk=snapshot.pk)
        if audit.run_id != snapshot.run_id or (
            revision is not None and revision != audit.revision
        ):
            raise WorkflowConflict("Finance audit revision changed.")
        return cls.authorize(user, audit)

    @staticmethod
    def changed(audit, population=False):
        audit.revision += 1
        if population:
            audit.population_revision += 1
        audit.save(update_fields=["revision", "population_revision", "updated_at"])
        if audit.run_id and audit.run.status not in WorkflowEngineService.CLOSED:
            audit.run.revision += 1
            audit.run.metadata = {
                **audit.run.metadata,
                "audit_revision": audit.revision,
            }
            audit.run.save(update_fields=["revision", "metadata", "updated_at"])

    @staticmethod
    def editor(user, audit, before_start=False):
        if before_start and (
            not audit.run_id or audit.run.status in WorkflowEngineService.CLOSED
        ):
            WorkflowDefinitionService.require_admin(user, audit.department_id)
            return
        if (
            not audit.run_id
            or audit.run.status != "review_required"
            or audit.run.current_step.key != "review_audit"
        ):
            raise WorkflowConflict("Finance changes require the active audit review.")
        step = audit.run.step_runs.get(step=audit.run.current_step)
        WorkflowPermissionService.require(user, audit.run, "review", step)

    @classmethod
    @transaction.atomic
    def create(cls, user, *, title, scope, policy=None, department=None):
        WorkflowDefinitionService.require_admin(user, department)
        audit = FinanceAudit.objects.create(
            company=user.company,
            department_id=department,
            title=title,
            scope=schema.scope(scope),
            policy=schema.policy(policy or {}),
            created_by=user,
        )
        event(user, audit, "created")
        return audit

    @classmethod
    @transaction.atomic
    def configure(cls, user, audit, *, revision, scope, policy):
        audit = cls.locked(user, audit, revision)
        WorkflowDefinitionService.require_admin(user, audit.department_id)
        if audit.run_id and audit.run.status not in WorkflowEngineService.CLOSED:
            raise WorkflowConflict(
                "Scope and policy are frozen while an audit workflow is active."
            )
        audit.scope, audit.policy = schema.scope(scope), schema.policy(policy)
        audit.save(update_fields=["scope", "policy", "updated_at"])
        cls.changed(audit, True)
        event(user, audit, "scope_configured")
        return audit

    @classmethod
    @transaction.atomic
    @exact_finance
    def ingest(
        cls,
        user,
        audit,
        *,
        revision,
        document_uuid,
        kind,
        mapping,
        idempotency_key,
        control_totals=None,
    ):
        audit = cls.locked(user, audit)
        cls.editor(user, audit, before_start=True)
        previous = audit.datasets.filter(idempotency_key=idempotency_key).first()
        if previous:
            if (
                str(previous.document.uuid) != str(document_uuid)
                or previous.kind != kind
                or previous.mapping != mapping
                or previous.totals.get("declared", {}) != (control_totals or {})
            ):
                raise WorkflowConflict(
                    "Dataset idempotency key was reused with another command."
                )
            return previous
        if audit.revision != revision:
            raise WorkflowConflict()
        doc = Document.objects.filter(company=user.company, uuid=document_uuid).first()
        if not doc:
            raise PermissionDenied("Dataset source is unavailable.")
        cls.document(user, doc)
        cls.source_limit(audit, doc)
        raw = cls.raw(doc)
        checksum = hashlib.sha256(raw).hexdigest()
        dataset = FinanceDataset(
            audit=audit,
            document=doc,
            number=number(audit.datasets),
            checksum=checksum,
            governance_revision=doc.governance_revision,
            kind=kind,
            mapping=mapping,
            row_count=0,
            actor=user,
            idempotency_key=idempotency_key,
        )
        rows = []
        totals = defaultdict(
            lambda: {
                "gross_amount": Decimal(0),
                "debit": Decimal(0),
                "credit": Decimal(0),
            }
        )
        missing_gross = Counter()
        for index, data, names in schema.parse(raw, kind, mapping):
            if not cls.in_scope(audit, data):
                raise ValidationError(
                    "Dataset row is outside configured population dimensions."
                )
            for key in totals[data["currency"]]:
                totals[data["currency"]][key] += total_value(data, key) or Decimal(0)
            if data.get("gross_amount") is None:
                missing_gross[data["currency"]] += 1
            rows.append((index, data, names))
        if (
            FinanceTransaction.objects.filter(dataset__audit=audit).count() + len(rows)
            > 100000
        ):
            raise ValidationError(
                "Audit population limit reached; split the engagement."
            )
        computed = {
            currency: {key: str(amount) for key, amount in values.items()}
            for currency, values in totals.items()
        }
        for currency in missing_gross:
            computed[currency]["gross_amount"] = None
        declared = control_totals or {}
        if not isinstance(declared, dict) or len(declared) > 100:
            raise ValidationError("Invalid declared controls.")
        for currency, values in declared.items():
            if (
                currency not in computed
                or not isinstance(values, dict)
                or set(values) - {"gross_amount", "debit", "credit"}
            ):
                raise ValidationError("Invalid declared control currency or field.")
            if any(
                computed[currency][key] is None
                or schema.money(value, False) != Decimal(computed[currency][key])
                for key, value in values.items()
            ):
                raise ValidationError("Dataset control totals do not reconcile.")
        dataset.row_count = len(rows)
        dataset.totals = {
            "computed": computed,
            "declared": declared,
            "validation": "validated",
            "missing_gross_amount": dict(missing_gross),
        }
        dataset.save()
        FinanceTransaction.objects.bulk_create(
            [
                FinanceTransaction(
                    dataset=dataset,
                    row=index,
                    identifier=data["transaction_id"],
                    transaction_date=data["transaction_date"],
                    transaction_type=data["transaction_type"],
                    currency=data["currency"],
                    amount=data.get("gross_amount"),
                    vendor=data.get("vendor_identifier", ""),
                    invoice_reference=data.get("invoice_reference", ""),
                    payment_reference=data.get("payment_reference", ""),
                    data=data,
                    provenance={
                        "row": index,
                        "sheet": None,
                        "schema_version": 1,
                        "checksum": checksum,
                        "source_fields": names,
                    },
                )
                for index, data, names in rows
            ],
            batch_size=500,
        )
        cls.pin_run(audit, doc)
        cls.changed(audit, True)
        event(user, audit, "dataset_imported")
        return dataset

    @staticmethod
    def in_scope(audit, data):
        for scope_key, field in (
            ("transaction_types", "transaction_type"),
            ("vendors", "vendor_identifier"),
            ("accounts", "account_code"),
            ("cost_centers", "cost_center"),
            ("business_units", "business_unit"),
        ):
            if (
                audit.scope.get(scope_key)
                and data.get(field) not in audit.scope[scope_key]
            ):
                return False
        return True

    @staticmethod
    def source_limit(audit, doc):
        ids = set(audit.datasets.values_list("document_id", flat=True)) | set(
            audit.evidence_links.values_list("document_id", flat=True)
        )
        if doc.pk not in ids and len(ids) >= 20:
            raise ValidationError("At most twenty distinct source documents per audit.")

    @staticmethod
    def pin_run(audit, doc):
        if audit.run_id and audit.run.status not in WorkflowEngineService.CLOSED:
            WorkflowRunDocument.objects.get_or_create(
                run=audit.run,
                document=doc,
                defaults={
                    "purpose": "finance_evidence",
                    "governance_revision": doc.governance_revision,
                },
            )

    @classmethod
    def rows(cls, audit):
        return (
            FinanceTransaction.objects.filter(dataset__audit=audit)
            .select_related("dataset")
            .prefetch_related(
                Prefetch(
                    "corrections",
                    queryset=FinanceCorrection.objects.order_by("-number")[:1],
                    to_attr="latest_correction",
                ),
                "evidence_links",
            )
            .order_by("pk")
        )

    @staticmethod
    def effective(row):
        correction = next(iter(row.latest_correction), None)
        return (correction.corrected if correction else row.data), correction

    @classmethod
    def population_digest(cls, audit):
        hasher = hashlib.sha256()
        for row in cls.rows(audit).iterator(chunk_size=500):
            data, correction = cls.effective(row)
            hasher.update(
                (
                    str(row.uuid)
                    + digest(data)
                    + (str(correction.uuid) if correction else "")
                ).encode()
            )
        return hasher.hexdigest()

    @classmethod
    @transaction.atomic
    def correct(cls, user, audit, item, *, revision, values, reason):
        audit = cls.locked(user, audit, revision)
        cls.editor(user, audit, before_start=True)
        row = cls.rows(audit).get(uuid=item)
        original, _ = cls.effective(row)
        corrected = schema.record(values)
        if not cls.in_scope(audit, corrected):
            raise ValidationError(
                "Correction falls outside configured population dimensions."
            )
        correction = FinanceCorrection.objects.create(
            transaction=row,
            number=number(row.corrections),
            original=original,
            corrected=corrected,
            actor=user,
            reason=reason,
        )
        cls.changed(audit, True)
        event(user, audit, "transaction_corrected")
        return correction

    @classmethod
    def link(cls, user, audit, row, doc, role, data=None):
        cls.document(user, doc)
        cls.source_limit(audit, doc)
        old = FinanceEvidenceLink.objects.filter(
            audit=audit, transaction=row, document=doc, role=role
        ).first()
        if old:
            if old.data != (data or {}):
                raise WorkflowConflict("Supporting evidence is immutable.")
            return old
        link = FinanceEvidenceLink.objects.create(
            audit=audit,
            transaction=row,
            document=doc,
            role=role,
            checksum=doc.checksum,
            text_digest=text_digest(doc),
            file_digest=hashlib.sha256(cls.raw(doc)).hexdigest() if doc.file else "",
            governance_revision=doc.governance_revision,
            data=data or {},
            actor=user,
        )
        cls.pin_run(audit, doc)
        return link

    @classmethod
    @transaction.atomic
    def support(cls, user, audit, item, *, revision, document_uuid, role, evidence):
        audit = cls.locked(user, audit, revision)
        cls.editor(user, audit, before_start=True)
        row = FinanceTransaction.objects.get(dataset__audit=audit, uuid=item)
        doc = Document.objects.filter(company=user.company, uuid=document_uuid).first()
        if not doc:
            raise PermissionDenied("Supporting document is unavailable.")
        if role not in {"receipt", "purchase_order", "support", "approval", "contract"}:
            raise ValidationError("Invalid supporting-document role.")
        cls.document(user, doc)
        # Values must be backed by exact stored source offsets; a document UUID alone is not proof.
        from apps.workflows.services.contract_extraction_service import (
            validate_evidence,
        )
        from apps.workflows.services.clause_detection_service import (
            ClauseDetectionService,
        )

        text, coverage = ClauseDetectionService.source(doc)
        if (
            not coverage["complete"]
            or not isinstance(evidence, dict)
            or set(evidence) - {"proof", "values"}
        ):
            raise ValidationError(
                "Supporting financial evidence requires source proof."
            )
        bounded_json(evidence)
        proof = evidence.get("proof")
        if (
            not isinstance(proof, dict)
            or type(proof.get("start")) is not int
            or type(proof.get("end")) is not int
            or proof["end"] - proof["start"] > 2000
            or proof.get("governance_revision") != doc.governance_revision
        ):
            raise ValidationError(
                "A bounded exact source span at the current governance revision is required."
            )
        validate_evidence(doc, [evidence.get("proof")])
        values = evidence.get("values", {})
        if not isinstance(values, dict) or set(values) - {
            "reference",
            "amount",
            "currency",
            "quantity",
            "unit_price",
            "date",
        }:
            raise ValidationError("Invalid supporting financial facts.")
        proof = evidence["proof"]
        excerpt = text[proof["start"] : proof["end"]]
        for key, value in values.items():
            if (
                not isinstance(value, str)
                or not value
                or len(value) > 150
                or not re.search(
                    r"(?<![\w.])" + re.escape(value) + r"(?![\w.])", excerpt
                )
            ):
                raise ValidationError("Financial fact is absent from the source proof.")
            if key in {"amount", "quantity", "unit_price"}:
                schema.money(value, False)
            if key == "date":
                schema.iso_date(value)
        link = cls.link(user, audit, row, doc, role, evidence)
        cls.changed(audit, True)
        event(user, audit, "support_linked")
        return link

    @classmethod
    @transaction.atomic
    @exact_finance
    def sample(
        cls, user, audit, *, revision, parameters, idempotency_key, internal=False
    ):
        audit = cls.locked(user, audit)
        if not internal:
            cls.editor(user, audit, before_start=True)
        parameters = schema.sampling(parameters)
        old = audit.samples.filter(idempotency_key=idempotency_key).first()
        if old:
            if old.parameters != parameters:
                raise WorkflowConflict()
            return old
        if audit.revision != revision:
            raise WorkflowConflict()
        method, size = parameters["method"], parameters["size"]

        def candidates():
            for row in cls.rows(audit).iterator(chunk_size=500):
                data, _ = cls.effective(row)
                if (
                    method in {"high_value", "value_threshold"}
                    and data.get("gross_amount") is None
                ):
                    continue
                amount = abs(Decimal(data.get("gross_amount") or "0"))
                currency = data["currency"]
                threshold = parameters.get("thresholds", {}).get(currency)
                if method == "value_threshold" and (
                    threshold is None or amount < Decimal(threshold)
                ):
                    continue
                hashed = hashlib.sha256(
                    (parameters.get("seed", "") + str(row.uuid)).encode()
                ).hexdigest()
                risk = int(not data.get("invoice_reference")) + int(
                    data.get("manual_entry", False)
                )
                # Currency amounts are never ranked across currencies without FX.
                yield row, data, amount, hashed, risk

        count = FinanceTransaction.objects.filter(dataset__audit=audit).count()
        if method == "systematic":
            chosen = list(
                islice(
                    (
                        x
                        for i, x in enumerate(candidates())
                        if i % max(1, count // size) == 0
                    ),
                    size,
                )
            )
        elif method in {"high_value", "value_threshold", "stratified", "risk_based"}:
            strata = Counter(x[1]["currency"] for x in candidates())
            if len(strata) > 100:
                raise ValidationError("Sampling currency strata limit exceeded.")
            quotas = dict.fromkeys(sorted(strata), 0)
            allocated = 0
            target = min(size, sum(strata.values()))
            while allocated < target:
                for currency in quotas:
                    if allocated < target and quotas[currency] < strata[currency]:
                        quotas[currency] += 1
                        allocated += 1
            groups = defaultdict(list)
            for x in candidates():
                currency = x[1]["currency"]
                quota = quotas[currency]
                if not quota:
                    continue
                rank = (
                    x[3]
                    if method == "stratified"
                    else (-x[4], -x[2], str(x[0].uuid))
                    if method == "risk_based"
                    else (-x[2], str(x[0].uuid))
                )
                groups[currency].append((rank, x))
                if len(groups[currency]) > quota:
                    groups[currency] = heapq.nsmallest(
                        quota, groups[currency], key=lambda y: y[0]
                    )
            # Round-robin strata avoids inventing FX equivalence.
            sorted_groups = [
                sorted(group, key=lambda y: y[0]) for _, group in sorted(groups.items())
            ]
            chosen = []
            for i in range(size):
                for group in sorted_groups:
                    if i < len(group) and len(chosen) < size:
                        chosen.append(group[i][1])
        else:
            chosen = heapq.nsmallest(size, candidates(), key=lambda x: x[3])
        if not chosen:
            raise ValidationError(
                "No eligible transactions for this sample configuration."
            )
        sample = FinanceSample.objects.create(
            audit=audit,
            population_revision=audit.population_revision,
            population_digest=cls.population_digest(audit),
            method=method,
            parameters=parameters,
            selected=[
                {
                    "transaction_uuid": str(x[0].uuid),
                    "reason": method,
                    "currency": x[1]["currency"],
                    "signals": {
                        "missing_invoice_reference": not bool(
                            x[1].get("invoice_reference")
                        ),
                        "manual_entry": bool(x[1].get("manual_entry")),
                    }
                    if method == "risk_based"
                    else {},
                }
                for x in chosen
            ],
            actor=user,
            idempotency_key=idempotency_key,
        )
        event(user, audit, "sample_generated")
        return sample

    @classmethod
    def invoice(cls, user, audit, row, data):
        if data["transaction_type"] not in {"payment", "invoice", "expense"}:
            return {"status": "not_applicable"}, None
        reference = data.get("invoice_reference")
        if not reference:
            return {"status": "not_found"}, None
        rows = list(
            accessible_invoice_records(user)
            .filter(invoice_number=reference)
            .select_related("document")
            .order_by("pk")[:21]
        )
        candidates = []
        for invoice in rows:
            cls.document(user, invoice.document)
            normalized = invoice.normalized
            vendor = data.get("vendor_identifier")
            if vendor and normalized.get("vendor_identifier") != vendor:
                continue
            if (
                data.get("vendor_tax_id")
                and normalized.get("vendor_tax_id")
                and data["vendor_tax_id"] != normalized["vendor_tax_id"]
            ):
                continue
            candidates.append(invoice)
        if len(rows) > 20 or len(candidates) > 1:
            return {"status": "ambiguous"}, None
        if not candidates:
            return {"status": "not_found", "vendor_mismatch": bool(rows)}, None
        invoice = candidates[0]
        pin = {
            "invoice_uuid": str(invoice.uuid),
            "invoice_digest": digest(invoice.normalized),
            "invoice_revision": invoice.revision,
        }
        cls.link(user, audit, row, invoice.document, "invoice", pin)
        status = (
            "matched"
            if data.get("vendor_identifier")
            and invoice.normalized.get("vendor_identifier") == data["vendor_identifier"]
            else "probable_match"
        )
        return {"status": status, **pin}, invoice

    @classmethod
    @exact_finance
    def checks(cls, user, audit, row, data, peers):
        amount = Decimal(data.get("gross_amount") or "0")
        currency = data["currency"]
        tolerance = Decimal(audit.policy.get("tolerance", {}).get(currency, "0"))
        materiality = audit.policy.get("materiality", {}).get(currency)
        results = {}

        def add(code, status, **evidence):
            results[code] = {
                "status": status,
                "evidence": {
                    "dataset_uuid": str(row.dataset.uuid),
                    "transaction_uuid": str(row.uuid),
                    "row": row.row,
                    "source_checksum": row.dataset.checksum,
                    "rule_version": 1,
                    **evidence,
                },
            }

        missing = [
            key for key in ("account_code", "vendor_identifier") if not data.get(key)
        ]
        add("integrity", "review_required" if missing else "passed", missing=missing)
        tx_date = schema.iso_date(data["transaction_date"])
        outside = (
            not schema.iso_date(audit.scope["start"])
            <= tx_date
            <= schema.iso_date(audit.scope["end"])
        )
        add(
            "period",
            "exception" if outside or tx_date > timezone.localdate() else "passed",
            transaction_date=data["transaction_date"],
            start=audit.scope["start"],
            end=audit.scope["end"],
            future_dated=tx_date > timezone.localdate(),
        )
        cutoff_days = audit.policy.get("cutoff_days")
        posting_days = audit.policy.get("posting_days")
        posting_gap = (
            abs(
                (
                    schema.iso_date(data["posting_date"])
                    - schema.iso_date(
                        data.get("document_date") or data["transaction_date"]
                    )
                ).days
            )
            if data.get("posting_date")
            else None
        )
        near_end = (
            cutoff_days is not None
            and abs((schema.iso_date(audit.scope["end"]) - tx_date).days) <= cutoff_days
        )
        add(
            "cutoff",
            "review_required"
            if near_end
            or (
                posting_gap is not None
                and posting_days is not None
                and posting_gap > posting_days
            )
            else "passed",
            posting_gap_days=posting_gap,
            cutoff_days=cutoff_days,
            near_period_end=near_end,
        )
        add(
            "amount",
            "unavailable"
            if materiality is not None and data.get("gross_amount") is None
            else "exception"
            if materiality is not None and abs(amount) > Decimal(materiality)
            else "not_configured"
            if materiality is None
            else "passed",
            amount=str(amount) if data.get("gross_amount") is not None else None,
            currency=currency,
            threshold=materiality,
        )
        identifier_count = peers["identifiers"][data["transaction_id"]]
        payment_key = (
            data.get("payment_reference"),
            data.get("vendor_identifier"),
            currency,
            amount,
        )
        invoice_key = (
            data.get("invoice_reference"),
            data.get("vendor_identifier"),
            currency,
            amount,
        )
        exact = bool(
            data.get("gross_amount") is not None
            and data.get("payment_reference")
            and data.get("vendor_identifier")
            and peers["payments"][payment_key] > 1
        )
        probable = bool(
            data.get("gross_amount") is not None
            and data.get("invoice_reference")
            and data.get("vendor_identifier")
            and peers["invoices"][invoice_key] > 1
        )
        if probable and "duplicate_days" in audit.policy:
            dates = peers["invoice_dates"][invoice_key]
            probable = (
                sum(
                    abs((other - tx_date).days) <= audit.policy["duplicate_days"]
                    for other in dates
                )
                > 1
            )
        duplicate = (
            "exact_duplicate_candidate"
            if exact
            else "probable_duplicate"
            if probable
            else "duplicate_identifier"
            if identifier_count > 1
            else "none"
        )
        add(
            "duplicates",
            "review_required" if duplicate != "none" else "passed",
            classification=duplicate,
            identifier_count=identifier_count,
            conclusion="candidate_only",
        )
        required = data["transaction_type"] in audit.policy.get(
            "required_support_types", []
        )
        links = list(row.evidence_links.all())
        add(
            "support",
            "exception"
            if required and not links
            else "passed"
            if required
            else "not_applicable",
            required=required,
            evidence_link_uuids=[str(x.uuid) for x in links],
        )
        match, invoice = (
            cls.invoice(user, audit, row, data)
            if "invoice" in audit.policy["tests"]
            or "three_way" in audit.policy["tests"]
            else ({"status": "not_applicable"}, None)
        )
        if invoice:
            values = invoice.normalized
            total = values.get("grand_total")
            if values.get("currency") != currency:
                reconciliation = "currency_comparison_unavailable"
                calculation = {
                    "currency": currency,
                    "invoice_currency": values.get("currency"),
                    "fx": "unavailable",
                }
            elif total is None or data.get("gross_amount") is None:
                reconciliation, calculation = "insufficient_data", {}
            else:
                delta = amount - Decimal(total)
                reconciliation = (
                    "matched"
                    if abs(delta) <= tolerance
                    else "overpayment"
                    if delta > 0
                    else "partial_payment"
                    if data["transaction_type"] == "payment"
                    else "amount_mismatch"
                )
                calculation = {
                    "invoice_total": total,
                    "transaction_amount": str(amount),
                    "difference": str(delta),
                    "tolerance": str(tolerance),
                    "currency": currency,
                }
            early_payment = bool(
                data["transaction_type"] == "payment"
                and values.get("invoice_date")
                and data["transaction_date"] < values["invoice_date"]
            )
            add(
                "invoice",
                "passed"
                if match["status"] == "matched"
                and reconciliation == "matched"
                and not early_payment
                else "review_required",
                match=match,
                reconciliation=reconciliation,
                calculation=calculation,
                payment_before_invoice=early_payment,
            )
            vendor = InvoiceMatchingService.vendor(invoice)
            add(
                "vendor",
                "passed" if vendor["status"] == "matched" else "review_required",
                vendor=vendor,
            )
        else:
            add(
                "invoice",
                "not_applicable"
                if match["status"] == "not_applicable"
                else "review_required",
                match=match,
            )
            from types import SimpleNamespace

            vendor = InvoiceMatchingService.vendor(
                SimpleNamespace(
                    company_id=audit.company_id,
                    normalized={
                        "vendor_identifier": data.get("vendor_identifier", ""),
                        "vendor_tax_id": data.get("vendor_tax_id", ""),
                        "vendor_name": data.get("vendor_name", ""),
                    },
                )
            )
            add(
                "vendor",
                "passed" if vendor["status"] == "matched" else "review_required",
                vendor=vendor,
                vendor_mismatch=match.get("vendor_mismatch", False),
            )
        supporting = {
            x.role: x for x in links if x.role in {"purchase_order", "receipt"}
        }
        if not data.get("purchase_order_reference"):
            add("three_way", "not_applicable")
        elif not invoice or set(supporting) != {"purchase_order", "receipt"}:
            add(
                "three_way",
                "unavailable",
                missing=[
                    x
                    for x in ("invoice", "purchase_order", "receipt")
                    if (x == "invoice" and not invoice)
                    or (x != "invoice" and x not in supporting)
                ],
            )
        else:
            po, receipt = (
                supporting["purchase_order"].data["values"],
                supporting["receipt"].data["values"],
            )
            checks = []
            if po.get("reference") != data["purchase_order_reference"]:
                checks.append("reference_mismatch")
            if receipt.get("reference") != data["purchase_order_reference"]:
                checks.append("receipt_reference_mismatch")
            if invoice.normalized.get("currency") != currency:
                checks.append("invoice_currency_mismatch")
            if invoice.normalized.get("grand_total") is None:
                checks.append("invoice_amount_unavailable")
            if (
                invoice.normalized.get("purchase_order_number")
                and invoice.normalized["purchase_order_number"]
                != data["purchase_order_reference"]
            ):
                checks.append("invoice_po_reference_mismatch")
            invoice_lines = invoice.normalized.get("line_items") or []
            invoice_quantity = (
                sum((Decimal(line["quantity"]) for line in invoice_lines), Decimal(0))
                if invoice_lines
                and all(line.get("quantity") is not None for line in invoice_lines)
                else None
            )
            if invoice_quantity is not None:
                for role, facts in (("po", po), ("receipt", receipt)):
                    if (
                        facts.get("quantity") is not None
                        and Decimal(facts["quantity"]) != invoice_quantity
                    ):
                        checks.append(role + "_invoice_quantity_mismatch")
            invoice_price = (
                invoice_lines[0].get("unit_price") if len(invoice_lines) == 1 else None
            )
            if invoice_price is not None:
                for role, facts in (("po", po), ("receipt", receipt)):
                    if (
                        facts.get("unit_price") is not None
                        and abs(Decimal(facts["unit_price"]) - Decimal(invoice_price))
                        > tolerance
                    ):
                        checks.append(role + "_invoice_unit_price_mismatch")
            if (
                po.get("unit_price") is not None
                and receipt.get("unit_price") is not None
                and Decimal(po["unit_price"]) != Decimal(receipt["unit_price"])
            ):
                checks.append("unit_price_mismatch")
            for facts in (po, receipt):
                if facts.get("currency") != currency:
                    checks.append("currency_unavailable")
                if facts.get("amount") is None:
                    checks.append("amount_unavailable")
                elif (
                    abs(
                        Decimal(facts["amount"])
                        - Decimal(invoice.normalized.get("grand_total") or "0")
                    )
                    > tolerance
                ):
                    checks.append("amount_mismatch")
            if (
                po.get("quantity") is not None
                and receipt.get("quantity") is not None
                and Decimal(receipt["quantity"]) < Decimal(po["quantity"])
            ):
                checks.append("partial_receipt")
            add(
                "three_way",
                "review_required" if checks else "passed",
                checks=checks,
                purchase_order=po,
                receipt=receipt,
                tolerance=str(tolerance),
                invoice_quantity=str(invoice_quantity)
                if invoice_quantity is not None
                else None,
                invoice_unit_price=invoice_price,
                purchase_order_link_uuid=str(supporting["purchase_order"].uuid),
                receipt_link_uuid=str(supporting["receipt"].uuid),
            )
        lines = journal_lines(data)
        debit = sum((Decimal(x.get("debit") or "0") for x in lines), Decimal(0))
        credit = sum((Decimal(x.get("credit") or "0") for x in lines), Decimal(0))
        has_entry = any(
            x.get("debit") is not None or x.get("credit") is not None for x in lines
        )
        add(
            "journal",
            "exception"
            if has_entry and abs(debit - credit) > tolerance
            else "passed"
            if has_entry
            else "not_applicable",
            debit=str(debit),
            credit=str(credit),
            difference=str(debit - credit),
            tolerance=str(tolerance),
        )
        frequency = peers["vendors"][data.get("vendor_identifier", "")]
        limit = audit.policy.get("frequency_limit")
        add(
            "frequency",
            "review_required"
            if limit is not None and frequency > limit
            else "passed"
            if limit is not None
            else "not_configured",
            population_frequency=frequency,
            configured_limit=limit,
            conclusion="anomaly_only",
        )
        return {code: results[code] for code in audit.policy["tests"]}

    @classmethod
    @transaction.atomic
    def execute(
        cls,
        user,
        audit,
        *,
        revision,
        idempotency_key,
        sample_uuid=None,
        previous_uuid=None,
        internal=False,
    ):
        audit = cls.locked(user, audit)
        if not internal:
            cls.editor(user, audit)
        old = audit.test_runs.filter(idempotency_key=idempotency_key).first()
        if old:
            if (str(old.sample.uuid) if old.sample_id else None) != (
                str(sample_uuid) if sample_uuid else None
            ) or (
                previous_uuid
                and (
                    not old.previous_id or str(old.previous.uuid) != str(previous_uuid)
                )
            ):
                raise WorkflowConflict()
            return old
        if audit.revision != revision or not audit.run_id:
            raise WorkflowConflict()
        started = timezone.now()
        population_digest = cls.population_digest(audit)
        sample = audit.samples.filter(uuid=sample_uuid).first() if sample_uuid else None
        if sample_uuid and (
            not sample
            or sample.population_revision != audit.population_revision
            or sample.population_digest != population_digest
        ):
            raise WorkflowConflict("Sample population is stale.")
        previous = (
            audit.test_runs.filter(uuid=previous_uuid).first()
            if previous_uuid
            else audit.test_runs.order_by("-pk").first()
        )
        if previous_uuid and not previous:
            raise ValidationError("Retest parent is unavailable.")
        query = cls.rows(audit)
        if sample:
            query = query.filter(
                uuid__in=[x["transaction_uuid"] for x in sample.selected]
            )
        if query.count() > 10000:
            raise ValidationError(
                "Use a bounded sample for populations above 10,000 transactions."
            )
        peers = {
            key: Counter() for key in ("identifiers", "payments", "invoices", "vendors")
        }
        peers["invoice_dates"] = defaultdict(list)
        for row in cls.rows(audit).iterator(chunk_size=500):
            data, _ = cls.effective(row)
            peers["identifiers"][data["transaction_id"]] += 1
            peers["vendors"][data.get("vendor_identifier", "")] += 1
            for key, field in (
                ("payments", "payment_reference"),
                ("invoices", "invoice_reference"),
            ):
                peer_key = (
                    data.get(field),
                    data.get("vendor_identifier"),
                    data["currency"],
                    Decimal(data.get("gross_amount") or "0"),
                )
                peers[key][peer_key] += 1
                if key == "invoices" and "duplicate_days" in audit.policy:
                    peers["invoice_dates"][peer_key].append(
                        schema.iso_date(data["transaction_date"])
                    )
        step = audit.run.step_runs.get(step__key="perform_audit")
        results = []
        counts = Counter()
        for row in query.iterator(chunk_size=200):
            data, correction = cls.effective(row)
            for code, result in cls.checks(user, audit, row, data, peers).items():
                counts[result["status"]] += 1
                results.append((row, correction, code, result))
        if (
            counts["exception"] + counts["review_required"] + counts["unavailable"]
            > 5000
        ):
            raise ValidationError("Finding limit reached; use a smaller sample.")
        test = FinanceTestRun.objects.create(
            audit=audit,
            run=audit.run,
            sample=sample,
            previous=previous,
            population_revision=audit.population_revision,
            population_digest=population_digest,
            definitions={
                "schema_version": 1,
                "library_version": 1,
                "policy": audit.policy,
                "scope": audit.scope,
            },
            counts=dict(counts),
            actor=user,
            started_at=started,
            completed_at=timezone.now(),
            idempotency_key=idempotency_key,
        )
        records = []
        for row, correction, code, result in results:
            finding = None
            if result["status"] in {"exception", "review_required", "unavailable"}:
                severity = audit.policy.get("severities", {}).get(
                    code, "high" if result["status"] == "exception" else "medium"
                )
                finding = WorkflowFinding.objects.create(
                    run=audit.run,
                    step_run=step,
                    category="finance_" + code,
                    severity=severity,
                    title="Finance " + code + " requires review",
                    data={
                        "audit_uuid": str(audit.uuid),
                        "test_run_uuid": str(test.uuid),
                        "transaction_uuid": str(row.uuid),
                        "rule": code,
                    },
                    evidence=[result["evidence"]],
                )
            records.append(
                FinanceTestResult(
                    test_run=test,
                    transaction=row,
                    correction=correction,
                    code=code,
                    status=result["status"],
                    evidence=result["evidence"],
                    finding=finding,
                )
            )
        FinanceTestResult.objects.bulk_create(records, batch_size=500)
        cls.changed(audit)
        event(user, audit, "tests_executed")
        notify_workflow(audit.run, user, "finance_review_required")
        return test

    @classmethod
    @transaction.atomic
    def request_evidence(
        cls,
        user,
        audit,
        finding_uuid,
        *,
        revision,
        requested_item,
        owner,
        due_date=None,
    ):
        from apps.accounts.models import User
        from apps.workflows.models import WorkflowComment

        audit = cls.locked(user, audit, revision)
        cls.editor(user, audit)
        finding = WorkflowFinding.objects.filter(
            run=audit.run, uuid=finding_uuid
        ).first()
        if (
            not finding
            or not FinanceTestResult.objects.filter(
                test_run__audit=audit, finding=finding
            ).exists()
        ):
            raise ValidationError("Finance finding is unavailable.")
        WorkflowDefinitionService.validate_assignment(audit.company_id, owner)
        recipient = User.objects.get(pk=owner["user"]) if owner.get("user") else None
        if recipient and not WorkflowPermissionService.content_allowed(
            recipient, audit.run
        ):
            raise PermissionDenied("Evidence request owner lacks source access.")
        if due_date:
            schema.iso_date(due_date)
        bounded_json({"requested_item": requested_item, "owner": owner}, 32768)
        value = FinanceManagementResponse.objects.create(
            audit=audit,
            finding=finding,
            number=number(audit.responses.filter(finding=finding)),
            actor=user,
            data={
                "state": "evidence_requested",
                "requested_item": requested_item,
                "owner": owner,
                "due_date": due_date,
            },
        )
        WorkflowComment.objects.create(
            run=audit.run,
            author=user,
            body="Finance evidence request "
            + str(value.uuid)
            + " for finding "
            + str(finding.uuid),
        )
        cls.changed(audit)
        event(user, audit, "evidence_requested")
        notify_workflow(
            audit.run, user, "evidence_requested", [recipient] if recipient else None
        )
        return value

    @classmethod
    @transaction.atomic
    def respond(
        cls,
        user,
        audit,
        finding_uuid,
        *,
        revision,
        response,
        planned_action,
        target_date=None,
        owner=None,
        evidence_documents=None,
        request_uuid=None,
    ):
        audit = cls.locked(user, audit, revision)
        cls.editor(user, audit)
        finding = WorkflowFinding.objects.get(run=audit.run, uuid=finding_uuid)
        if not FinanceTestResult.objects.filter(
            test_run__audit=audit, finding=finding
        ).exists():
            raise ValidationError("Finding is not part of this finance audit.")
        if (
            request_uuid
            and not audit.responses.filter(
                uuid=request_uuid, finding=finding, data__state="evidence_requested"
            ).exists()
        ):
            raise ValidationError("Evidence request is not part of this finding.")
        if owner:
            WorkflowDefinitionService.validate_assignment(audit.company_id, owner)
        if target_date:
            schema.iso_date(target_date)
        docs = evidence_documents or []
        if len(docs) > 20:
            raise ValidationError("Too many response evidence documents.")
        for uuid in docs:
            doc = Document.objects.filter(company=user.company, uuid=uuid).first()
            if not doc:
                raise PermissionDenied("Response evidence is unavailable.")
            cls.link(user, audit, None, doc, "management_response")
        value = FinanceManagementResponse.objects.create(
            audit=audit,
            finding=finding,
            number=number(audit.responses.filter(finding=finding)),
            actor=user,
            data={
                "response": response,
                "planned_action": planned_action,
                "target_date": target_date,
                "owner": owner or {},
                "evidence_documents": [str(x) for x in docs],
                "state": "management_response_received",
                "request_uuid": str(request_uuid) if request_uuid else None,
            },
        )
        cls.changed(audit)
        event(user, audit, "management_response_recorded")
        return value

    @classmethod
    @transaction.atomic
    def decide(cls, user, audit, finding_uuid, *, revision, decision, reason):
        audit = cls.locked(user, audit, revision)
        cls.editor(user, audit)
        finding = WorkflowFinding.objects.select_for_update().get(
            run=audit.run, uuid=finding_uuid
        )
        if (
            decision
            not in {
                "under_review",
                "remediation_in_progress",
                "resolved",
                "accepted_risk",
                "closed",
            }
            or not reason.strip()
        ):
            raise ValidationError("Invalid controlled finding decision.")
        if not FinanceTestResult.objects.filter(
            test_run__audit=audit, finding=finding
        ).exists():
            raise ValidationError("Finding is not part of this audit.")
        if (
            decision in {"resolved", "closed"}
            and not audit.responses.filter(
                finding=finding, data__state="management_response_received"
            ).exists()
        ):
            raise ValidationError("Closure requires a preserved management response.")
        if decision == "accepted_risk":
            WorkflowDefinitionService.require_admin(user, audit.department_id)
        value = FinanceFindingDecision.objects.create(
            audit=audit,
            finding=finding,
            number=number(audit.decisions.filter(finding=finding)),
            decision=decision,
            actor=user,
            reason=reason,
        )
        finding.status = (
            "accepted"
            if decision == "accepted_risk"
            else "resolved"
            if decision in {"resolved", "closed"}
            else "open"
        )
        finding.review_state = "reviewed"
        finding.save(update_fields=["status", "review_state", "updated_at"])
        cls.changed(audit)
        event(user, audit, "finding_" + decision)
        return value

    @classmethod
    def readiness(cls, user, audit):
        cls.authorize(user, audit)
        test = audit.test_runs.filter(run=audit.run).order_by("-pk").first()
        if (
            not test
            or test.population_revision != audit.population_revision
            or test.population_digest != cls.population_digest(audit)
        ):
            raise ValidationError(
                "Current population requires a completed audit test run."
            )
        if audit.run.exceptions.filter(status="open").exists():
            raise ValidationError("Audit workflow exceptions remain unresolved.")
        findings = WorkflowFinding.objects.filter(
            run=audit.run, severity__in=audit.policy["blocking_severities"]
        )
        for finding in findings.iterator():
            decision = (
                audit.decisions.filter(finding=finding).order_by("-number").first()
            )
            if (
                finding.status == "open"
                or not decision
                or decision.decision not in {"resolved", "closed", "accepted_risk"}
            ):
                raise ValidationError(
                    "Blocking finance findings require reviewed closure or accepted risk."
                )
        return test

    @classmethod
    @transaction.atomic
    @exact_finance
    def assemble(cls, audit):
        user = audit.run.initiated_by
        audit = cls.locked(user, audit)
        old = audit.outputs.filter(run=audit.run).first()
        if old:
            return old
        approval = (
            audit.run.step_runs.filter(step__key="approve_audit")
            .select_related("approval")
            .first()
        )
        if (
            audit.run.current_step.key != "finalize_audit"
            or not approval
            or not approval.approval_id
            or approval.approval.status != "approved"
        ):
            raise WorkflowConflict("Final finance output requires workflow approval.")
        test = cls.readiness(user, audit)
        findings = []
        for finding in audit.run.findings.order_by("pk").iterator():
            decision = (
                audit.decisions.filter(finding=finding).order_by("-number").first()
            )
            responses = list(
                audit.responses.filter(finding=finding)
                .order_by("number")
                .values("uuid", "number", "actor_id", "data")
            )
            findings.append(
                {
                    "uuid": str(finding.uuid),
                    "category": finding.category,
                    "severity": finding.severity,
                    "status": finding.status,
                    "evidence": finding.evidence,
                    "decision_uuid": str(decision.uuid) if decision else None,
                    "decision": decision.decision if decision else None,
                    "management_responses": [
                        {**x, "uuid": str(x["uuid"])} for x in responses
                    ],
                }
            )
        data = {
            "schema_version": 1,
            "assistance_only": True,
            "assurance": "No statutory opinion or financial-statement assurance is provided.",
            "scope": audit.scope,
            "policy": audit.policy,
            "population_revision": audit.population_revision,
            "population_digest": test.population_digest,
            "datasets": list(audit.datasets.values_list("uuid", flat=True)),
            "test_run_uuid": str(test.uuid),
            "counts": test.counts,
            "sample_uuid": str(test.sample.uuid) if test.sample_id else None,
            "findings": findings,
            "approval_uuid": str(approval.approval.uuid),
            "approval_run_uuid": str(audit.run.uuid),
        }
        data["datasets"] = [str(x) for x in data["datasets"]]
        data["dataset_snapshots"] = [
            {
                "uuid": str(row.uuid),
                "document_uuid": str(row.document.uuid),
                "checksum": row.checksum,
                "number": row.number,
                "row_count": row.row_count,
                "mapping": row.mapping,
                "totals": row.totals,
            }
            for row in audit.datasets.select_related("document").order_by("number")
        ]
        data["test_definitions"] = test.definitions
        data["sample"] = (
            {
                "method": test.sample.method,
                "parameters": test.sample.parameters,
                "selected": test.sample.selected,
            }
            if test.sample_id
            else None
        )
        currencies = defaultdict(
            lambda: {
                "transaction_count": 0,
                "missing_gross_amount": 0,
                "gross_amount": Decimal(0),
                "debit": Decimal(0),
                "credit": Decimal(0),
            }
        )
        for row in cls.rows(audit).iterator(chunk_size=500):
            values, _ = cls.effective(row)
            totals = currencies[values["currency"]]
            totals["transaction_count"] += 1
            if values.get("gross_amount") is None:
                totals["missing_gross_amount"] += 1
            for key in ("gross_amount", "debit", "credit"):
                totals[key] += total_value(values, key) or Decimal(0)
        for totals in currencies.values():
            if totals["missing_gross_amount"]:
                totals["gross_amount"] = None
        data["population_summary"] = {
            currency: {
                key: str(value) if isinstance(value, Decimal) else value
                for key, value in totals.items()
            }
            for currency, totals in currencies.items()
        }
        bounded_json(data, 8388608)
        output = FinanceAuditOutput.objects.create(
            audit=audit, run=audit.run, number=number(audit.outputs), data=data
        )
        event(user, audit, "finalized")
        notify_workflow(audit.run, user, "finance_audit_finalized")
        return output

    @classmethod
    def export(cls, user, audit, output):
        cls.authorize(user, audit, "export")
        if output.audit_id != audit.pk or output.run.status != "completed":
            raise WorkflowConflict("Approved finance output is unavailable.")
        event(user, audit, "exported")
        return {
            "output_uuid": str(output.uuid),
            "version": output.number,
            **output.data,
        }
