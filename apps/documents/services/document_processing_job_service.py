from apps.documents.models import DocumentProcessingJob


class DocumentProcessingJobService:
    DEFAULT_UPLOAD_JOBS = [
        DocumentProcessingJob.JobType.OCR,
        DocumentProcessingJob.JobType.CLASSIFICATION,
        DocumentProcessingJob.JobType.FIREWALL_SCAN,
        DocumentProcessingJob.JobType.CHUNKING,
        DocumentProcessingJob.JobType.EMBEDDING,
        DocumentProcessingJob.JobType.INDEXING,
    ]

    @classmethod
    def create_default_jobs_for_document(cls, document):
        jobs = [
            DocumentProcessingJob(
                document=document,
                job_type=job_type,
                status=DocumentProcessingJob.Status.PENDING,
            )
            for job_type in cls.DEFAULT_UPLOAD_JOBS
        ]

        return DocumentProcessingJob.objects.bulk_create(jobs)
