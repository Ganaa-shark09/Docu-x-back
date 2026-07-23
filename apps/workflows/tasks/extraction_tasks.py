from config.celery import app
from apps.workflows.models import ExtractionRun
from apps.workflows.services import StructuredExtractionService


@app.task(bind=True, max_retries=2)
def run_structured_extraction_task(self, extraction_run_uuid):
    extraction_run = ExtractionRun.objects.get(uuid=extraction_run_uuid)

    try:
        StructuredExtractionService.run_extraction(extraction_run)
        return {
            "extraction_run_uuid": str(extraction_run.uuid),
            "status": extraction_run.status,
            "overall_confidence": str(extraction_run.overall_confidence),
        }
    except Exception as exc:
        extraction_run.mark_failed(str(exc))
        raise self.retry(exc=exc, countdown=10)
