from django.apps import AppConfig


class WorkflowsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.workflows"

    def ready(self):
        from apps.workflows.services.compliance_workflow_service import (
            ComplianceProcessingAdapter,
        )
        from apps.workflows.services.finance_workflow_service import (
            FinanceProcessingAdapter,
        )

        from apps.workflows.services.rfp_workflow_service import RFPProcessingAdapter
        from apps.workflows.services.hr_policy_workflow_service import (
            HRPolicyProcessingAdapter,
        )
        from apps.workflows.services.clause_definition_service import (
            ClauseProcessingAdapter,
        )
        from apps.workflows.services.contract_workflow_service import (
            ContractProcessingAdapter,
        )
        from apps.workflows.services.invoice_workflow_service import (
            InvoiceProcessingAdapter,
        )
        from apps.workflows.services.workflow_processing_service import (
            WorkflowProcessingService,
        )

        WorkflowProcessingService.adapters["finance_audit"] = FinanceProcessingAdapter
        WorkflowProcessingService.adapters["compliance"] = ComplianceProcessingAdapter
        WorkflowProcessingService.adapters["invoice"] = InvoiceProcessingAdapter
        WorkflowProcessingService.adapters["rfp_proposal"] = RFPProcessingAdapter
        WorkflowProcessingService.adapters["hr_policy"] = HRPolicyProcessingAdapter
        WorkflowProcessingService.adapters["legal_clause_intelligence"] = (
            ClauseProcessingAdapter
        )
        WorkflowProcessingService.adapters["contract_review"] = (
            ContractProcessingAdapter
        )
