from celery import shared_task
from datetime import timedelta
from django.utils import timezone

from apps.workflows.models import WorkflowRun
from apps.workflows.services.workflow_executor_service import WorkflowExecutorService


@shared_task(bind=True, max_retries=3, acks_late=True, reject_on_worker_lost=True)
def execute_workflow(self, company_id, run_uuid, expected_revision):
    try:
        return WorkflowExecutorService.execute(
            company_id, run_uuid, expected_revision, attempt=self.request.retries + 1
        )
    except Exception:
        # Execution transaction rolls back on infrastructure failures. No raw
        # exception message or confidential payload enters task results/logs.
        run = (
            WorkflowRun.objects.filter(company_id=company_id, uuid=run_uuid)
            .select_related("version")
            .first()
        )
        max_attempts = run.version.configuration.get("max_attempts", 4) if run else 1
        if self.request.retries + 1 >= max_attempts:
            WorkflowExecutorService.fail(
                company_id,
                run_uuid,
                expected_revision,
                attempt=self.request.retries + 1,
            )
            return False
        raise self.retry(
            exc=RuntimeError("Workflow execution temporarily unavailable."),
            countdown=2 ** (self.request.retries + 1),
        ) from None


@shared_task
def dispatch_queued_workflows():
    # Recovery for a broker outage between database commit and task publication.
    # Revision checks make overlap with ordinary delivery harmless.
    for company_id, run_uuid, revision in (
        WorkflowRun.objects.filter(
            status="queued",
            is_deleted=False,
            updated_at__lte=timezone.now() - timedelta(seconds=30),
        )
        .order_by("updated_at")
        .values_list("company_id", "uuid", "revision")[:500]
        .iterator()
    ):
        execute_workflow.delay(company_id, str(run_uuid), revision)
