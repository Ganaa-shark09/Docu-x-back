from apps.ai_engine.services.external_intent_classifier_service import (
    ExternalIntentClassifierService,
)


class ExternalIntentRouterService:
    @classmethod
    def route(
        cls,
        message,
        has_attached_documents=False,
        explicit_document_uuids=None,
        use_web=True,
    ):
        return ExternalIntentClassifierService.classify(
            message=message,
            has_attached_documents=has_attached_documents,
            explicit_document_uuids=explicit_document_uuids,
            use_web=use_web,
        )
