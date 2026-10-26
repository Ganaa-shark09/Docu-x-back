from rest_framework.routers import DefaultRouter
from apps.workflows.views.source_proof_views import EvidenceViewSet, ProofClaimViewSet
from apps.workflows.views.compliance_views import (
    ComplianceFrameworkViewSet,
    ComplianceControlViewSet,
    ComplianceAssessmentViewSet,
)
from apps.workflows.views.finance_views import FinanceAuditViewSet
from apps.workflows.views.rfp_views import RFPProjectViewSet

from apps.workflows.views import ExtractionRunViewSet, ExtractionTemplateViewSet
from apps.workflows.views.invoice_views import InvoiceBatchViewSet, InvoiceViewSet
from apps.workflows.views.contract_views import ContractReviewViewSet
from apps.workflows.views.clause_views import (
    ClauseTypeViewSet,
    ClausePlaybookViewSet,
    ClauseAnalysisViewSet,
)

from apps.workflows.views.workflow_views import (
    WorkflowDefinitionViewSet,
    WorkflowRunViewSet,
    WorkflowVersionViewSet,
)

from apps.workflows.views.hr_policy_views import (
    HRPolicyViewSet,
    HRPolicyVersionViewSet,
    HRPolicyCategoryViewSet,
)

router = DefaultRouter()
router.register("evidence", EvidenceViewSet, basename="source-evidence")
router.register("proof-claims", ProofClaimViewSet, basename="proof-claims")
router.register(
    "compliance-frameworks",
    ComplianceFrameworkViewSet,
    basename="compliance-frameworks",
)
router.register(
    "compliance-controls", ComplianceControlViewSet, basename="compliance-controls"
)
router.register(
    "compliance-assessments",
    ComplianceAssessmentViewSet,
    basename="compliance-assessments",
)
router.register("finance-audits", FinanceAuditViewSet, basename="finance-audits")
router.register("rfp-projects", RFPProjectViewSet, basename="rfp-projects")
router.register("hr-policies", HRPolicyViewSet, basename="hr-policies")
router.register(
    "hr-policy-versions", HRPolicyVersionViewSet, basename="hr-policy-versions"
)
router.register(
    "hr-policy-categories", HRPolicyCategoryViewSet, basename="hr-policy-categories"
)
router.register("clause-types", ClauseTypeViewSet, basename="clause-types")
router.register("clause-playbooks", ClausePlaybookViewSet, basename="clause-playbooks")
router.register("clause-analyses", ClauseAnalysisViewSet, basename="clause-analyses")
router.register("contract-reviews", ContractReviewViewSet, basename="contract-reviews")

router.register("invoices", InvoiceViewSet, basename="invoices")
router.register("invoice-batches", InvoiceBatchViewSet, basename="invoice-batches")
router.register(
    "definitions", WorkflowDefinitionViewSet, basename="workflow-definitions"
)
router.register("versions", WorkflowVersionViewSet, basename="workflow-versions")
router.register("runs", WorkflowRunViewSet, basename="workflow-runs")
router.register(
    "extraction-templates", ExtractionTemplateViewSet, basename="extraction-templates"
)
router.register("extraction-runs", ExtractionRunViewSet, basename="extraction-runs")

urlpatterns = router.urls
