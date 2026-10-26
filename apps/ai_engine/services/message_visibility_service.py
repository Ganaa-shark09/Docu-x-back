from apps.documents.models import Document
from apps.documents.services import DocumentPermissionService


class AllowedProofDocuments(dict):
    # Lives only for the current serialization/history operation.
    proof_resolver = None


class MessageVisibilityService:
    OMITTED = "[Protected document context is no longer available.]"

    @staticmethod
    def dependencies(message):
        metadata = message.metadata or {}
        result = dict(metadata.get("document_dependencies") or {})
        for source in message.retrieved_sources or []:
            if isinstance(source, dict) and source.get("document_uuid"):
                result.setdefault(
                    str(source["document_uuid"]), source.get("governance_revision")
                )
        for key in ("attached_document_uuids", "requested_document_uuids"):
            for value in metadata.get(key) or []:
                result.setdefault(str(value), None)
        for action in (metadata.get("firewall_enforcement") or {}).get(
            "policy_actions", []
        ):
            if action.get("document_uuid"):
                result.setdefault(str(action["document_uuid"]), None)
        return result

    @classmethod
    def allowed_documents(cls, user, scope):
        return AllowedProofDocuments(
            {
                str(row.uuid): row.governance_revision
                for row in DocumentPermissionService.get_accessible_document_queryset(
                    user, require_ai=True, scope=scope
                ).only("uuid", "governance_revision")
            }
        )

    @classmethod
    def visible(cls, message, user, allowed=None):
        conversation = message.conversation
        if (
            conversation.company_id != user.company_id
            or conversation.user_id != user.pk
            or conversation.is_deleted
        ):
            return False
        if (message.metadata or {}).get("domain") == "hr_policy":
            from apps.workflows.services.hr_policy_qa_service import HRPolicyQAService

            if not HRPolicyQAService.lineage_visible(message, user):
                return False
        allowed = (
            cls.allowed_documents(user, conversation.scope)
            if allowed is None
            else allowed
        )
        dependencies = cls.dependencies(message)
        if (
            message.role == "assistant"
            and "document_dependencies" not in (message.metadata or {})
            and not dependencies
        ):
            # Legacy source-less derivatives have no trustworthy authorization
            # lineage. New general answers explicitly persist an empty lineage.
            return False
        if (message.metadata or {}).get(
            "intent"
        ) == "conversation_summary" and "document_dependencies" not in (
            message.metadata or {}
        ):
            # Historic summaries without source lineage cannot be reauthorized.
            return False
        from apps.ai_engine.services.internal_source_proof_service import (
            InternalSourceProofService,
        )

        return InternalSourceProofService.visible(message, user, allowed) and all(
            value in allowed and (revision is None or revision == allowed[value])
            for value, revision in dependencies.items()
        )

    @classmethod
    def capture(cls, message):
        """Snapshot transitive dependencies before persisting derived context.

        Includes source revisions from bounded memory, not a fresh revision for
        old answers: a revoked answer must never be laundered by a new summary.
        """
        conversation = message.conversation
        dependencies = cls.dependencies(message)
        previous = (
            conversation.messages.filter(is_deleted=False)
            .exclude(pk=message.pk)
            .order_by("-created_at")[:80]
        )
        for item in previous:
            for key, revision in cls.dependencies(item).items():
                dependencies.setdefault(key, revision)
        if conversation.scope == "external":
            for uuid in conversation.attached_documents.filter(
                is_deleted=False
            ).values_list("document__uuid", flat=True):
                dependencies.setdefault(str(uuid), None)
        if dependencies:
            current = {
                str(doc.uuid): doc.governance_revision
                for doc in Document.objects.filter(
                    company_id=conversation.company_id, uuid__in=dependencies
                )
            }
            dependencies = {
                key: current.get(key) if revision is None else revision
                for key, revision in dependencies.items()
            }
        return dependencies
