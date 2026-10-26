from django.db import transaction

from apps.ai_engine.models import AIConversation, AIMessage
from apps.ai_engine.services.internal_document_scope_service import (
    InternalDocumentScopeService,
)
from apps.ai_engine.services.internal_document_relevance_service import (
    InternalDocumentRelevanceService,
)
from apps.ai_engine.services.internal_general_answer_service import (
    InternalGeneralAnswerService,
)
from apps.ai_engine.services.llm_gateway_service import LLMGatewayService
from apps.ai_engine.services.llm_provider_config_service import LLMProviderConfigService
from apps.ai_engine.services.prompt_builder_service import PromptBuilderService
from apps.audit.services import AuditLogService
from apps.data_firewall.services import AIContextPolicyEnforcementService
from apps.documents.models import Document
from apps.vector_search.services import VectorSearchService


from apps.ai_engine.services.internal_conversation_memory_service import (
    InternalConversationMemoryService,
)
from apps.ai_engine.services.internal_conversation_summary_service import (
    InternalConversationSummaryService,
)


class RAGChatService:
    @classmethod
    @transaction.atomic
    def chat(
        cls,
        user,
        message,
        conversation_uuid=None,
        department_uuid=None,
        document_type=None,
        sensitivity_label=None,
        document_scope=InternalDocumentScopeService.ALL,
        document_uuids=None,
        top_k=5,
        scope=Document.Scope.INTERNAL,
        ip_address=None,
        user_agent="",
        domain=None,
        policy_context=None,
        idempotency_key=None,
    ):
        if scope != Document.Scope.INTERNAL:
            raise ValueError("RAGChatService only supports Internal AI document scope.")
        if domain == "hr_policy":
            from apps.workflows.services.hr_policy_qa_service import HRPolicyQAService

            return HRPolicyQAService.chat(
                user,
                message,
                context=policy_context,
                conversation_uuid=conversation_uuid,
                idempotency_key=idempotency_key,
                top_k=top_k,
            )

        if InternalConversationMemoryService.is_summary_request(message):
            conversation = cls._get_or_create_conversation(
                user=user,
                message=message,
                conversation_uuid=(conversation_uuid),
                department_uuid=(department_uuid),
                scope=scope,
            )

            user_message = AIMessage.objects.create(
                conversation=conversation,
                role=AIMessage.Role.USER,
                content=message,
                metadata={
                    "intent": "conversation_summary",
                    "document_retrieval_skipped": True,
                    "conversation_memory": True,
                },
            )

            return cls._answer_conversation_summary(
                user=user,
                conversation=conversation,
                user_message=user_message,
                message=message,
            )

        resolved_document_scope = InternalDocumentScopeService.resolve(
            user=user,
            document_scope=document_scope,
            document_uuids=document_uuids,
        )
        document_scope_metadata = resolved_document_scope.as_metadata()

        conversation = cls._get_or_create_conversation(
            user=user,
            message=message,
            conversation_uuid=conversation_uuid,
            department_uuid=department_uuid,
            scope=scope,
        )

        user_message = AIMessage.objects.create(
            conversation=conversation,
            role=AIMessage.Role.USER,
            content=message,
            metadata={
                "document_scope": document_scope_metadata,
            },
        )

        memory_history = InternalConversationMemoryService.build_history_messages(
            conversation=conversation,
            user=user,
            exclude_message_uuid=(user_message.uuid),
            mode="routing",
        )

        retrieval_query = InternalConversationMemoryService.resolve_retrieval_query(
            message=message,
            history=memory_history,
        )

        retrieved_chunks = VectorSearchService.search(
            company=user.company,
            user=user,
            query=retrieval_query,
            department_uuid=department_uuid,
            document_type=document_type,
            sensitivity_label=sensitivity_label,
            top_k=top_k,
            scope=scope,
            document_uuids=resolved_document_scope.search_document_uuids,
        )

        retrieved_chunks = InternalDocumentRelevanceService.filter_relevant_chunks(
            question=retrieval_query,
            retrieved_chunks=retrieved_chunks,
            force_document_scope=(
                resolved_document_scope.mode == InternalDocumentScopeService.SELECTED
            ),
        )

        requested_provider_config = (
            LLMProviderConfigService.get_default_config_for_company(user.company)
        )

        enforcement_result = AIContextPolicyEnforcementService.enforce(
            user=user,
            provider_config=requested_provider_config,
            retrieved_chunks=retrieved_chunks,
        )

        safe_chunks = enforcement_result["chunks"]
        effective_provider_config = enforcement_result["provider_config"]

        if not safe_chunks and enforcement_result.get("blocked_count", 0) == 0:
            return cls._answer_without_document_context(
                conversation=conversation,
                user_message=user_message,
                message=message,
                document_scope_metadata=document_scope_metadata,
            )

        prompt = PromptBuilderService.build_rag_prompt(
            user_question=retrieval_query,
            retrieved_chunks=safe_chunks,
            document_scope=resolved_document_scope.mode,
            selected_document_titles=resolved_document_scope.document_titles,
        )

        if not safe_chunks and enforcement_result.get("blocked_count", 0) > 0:
            llm_result = {
                "answer": (
                    "Relevant document context was found, but DocuX AI Data Firewall "
                    "blocked it because the content is restricted, highly sensitive, "
                    "or requires human approval before AI usage."
                ),
                "provider": "data_firewall",
                "model": "policy_enforcement",
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "metadata": {
                    "reason": "all_retrieved_context_blocked",
                },
            }
        else:
            llm_result = LLMGatewayService.generate_answer(
                question=message,
                retrieved_chunks=safe_chunks,
                prompt=prompt,
                provider_config=effective_provider_config,
            )

        assistant_message = AIMessage.objects.create(
            conversation=conversation,
            role=AIMessage.Role.ASSISTANT,
            content=llm_result["answer"],
            retrieved_sources=cls._build_sources(safe_chunks),
            model_provider=llm_result["provider"],
            model_name=llm_result["model"],
            prompt_tokens=llm_result["prompt_tokens"],
            completion_tokens=llm_result["completion_tokens"],
            total_tokens=llm_result["total_tokens"],
            metadata={
                "top_k": top_k,
                "document_type": document_type,
                "sensitivity_label": sensitivity_label,
                "department_uuid": str(department_uuid) if department_uuid else None,
                "document_scope": document_scope_metadata,
                "user_message_uuid": str(user_message.uuid),
                "llm_metadata": llm_result.get("metadata", {}),
                "firewall_enforcement": {
                    "requested_provider_type": enforcement_result.get(
                        "requested_provider_type"
                    ),
                    "effective_provider_type": enforcement_result.get(
                        "effective_provider_type"
                    ),
                    "forced_local": enforcement_result.get("forced_local"),
                    "allowed_count": enforcement_result.get("allowed_count"),
                    "blocked_count": enforcement_result.get("blocked_count"),
                    "redacted_count": enforcement_result.get("redacted_count"),
                    "policy_actions": enforcement_result.get("policy_actions"),
                },
            },
        )

        AuditLogService.log_ai_context_usage(
            user=user,
            conversation=conversation,
            assistant_message=assistant_message,
            requested_provider=enforcement_result.get("requested_provider_type"),
            effective_provider=assistant_message.model_provider,
            enforcement_result=enforcement_result,
            retrieved_chunks=retrieved_chunks,
            ip_address=ip_address,
            user_agent=user_agent,
        )

        conversation.save(update_fields=["updated_at"])

        return {
            "conversation": conversation,
            "user_message": user_message,
            "assistant_message": assistant_message,
            "sources": assistant_message.retrieved_sources,
            "document_context": document_scope_metadata,
            "firewall": {
                "requested_provider_type": enforcement_result.get(
                    "requested_provider_type"
                ),
                "effective_provider_type": enforcement_result.get(
                    "effective_provider_type"
                ),
                "forced_local": enforcement_result.get("forced_local"),
                "allowed_count": enforcement_result.get("allowed_count"),
                "blocked_count": enforcement_result.get("blocked_count"),
                "redacted_count": enforcement_result.get("redacted_count"),
                "policy_actions": enforcement_result.get("policy_actions"),
            },
        }

    @classmethod
    def _answer_without_document_context(
        cls,
        conversation,
        user_message,
        message,
        document_scope_metadata,
    ):
        llm_result = InternalGeneralAnswerService.generate(
            message=message,
            conversation=conversation,
            current_user_message_uuid=user_message.uuid,
        )

        assistant_message = AIMessage.objects.create(
            conversation=conversation,
            role=AIMessage.Role.ASSISTANT,
            content=llm_result["answer"],
            retrieved_sources=[],
            model_provider=llm_result["provider"],
            model_name=llm_result["model"],
            prompt_tokens=llm_result["prompt_tokens"],
            completion_tokens=llm_result["completion_tokens"],
            total_tokens=llm_result["total_tokens"],
            metadata={
                "document_scope": document_scope_metadata,
                "document_context_used": False,
                "user_message_uuid": str(user_message.uuid),
                "llm_metadata": llm_result.get("metadata", {}),
            },
        )

        conversation.save(update_fields=["updated_at"])

        return {
            "conversation": conversation,
            "user_message": user_message,
            "assistant_message": assistant_message,
            "sources": [],
            "document_context": document_scope_metadata,
            "firewall": {
                "requested_provider_type": None,
                "effective_provider_type": llm_result["provider"],
                "forced_local": llm_result["provider"] == "ollama",
                "allowed_count": 0,
                "blocked_count": 0,
                "redacted_count": 0,
                "policy_actions": [],
            },
        }

    @classmethod
    def _answer_conversation_summary(
        cls,
        user,
        conversation,
        user_message,
        message,
    ):
        llm_result = InternalConversationSummaryService.generate(
            user=user,
            conversation=conversation,
            current_user_message_uuid=(user_message.uuid),
            message=message,
        )

        assistant_message = AIMessage.objects.create(
            conversation=conversation,
            role=AIMessage.Role.ASSISTANT,
            content=llm_result["answer"],
            retrieved_sources=[],
            model_provider=(llm_result["provider"]),
            model_name=(llm_result["model"]),
            prompt_tokens=(
                llm_result.get(
                    "prompt_tokens",
                    0,
                )
            ),
            completion_tokens=(
                llm_result.get(
                    "completion_tokens",
                    0,
                )
            ),
            total_tokens=(
                llm_result.get(
                    "total_tokens",
                    0,
                )
            ),
            metadata={
                "intent": "conversation_summary",
                "document_retrieval_skipped": True,
                "conversation_memory": True,
                "memory_access_revalidated": True,
                "user_message_uuid": str(user_message.uuid),
                "llm_metadata": llm_result.get(
                    "metadata",
                    {},
                ),
            },
        )

        conversation.save(update_fields=["updated_at"])

        return {
            "conversation": conversation,
            "user_message": user_message,
            "assistant_message": assistant_message,
            "sources": [],
            "document_context": {
                "mode": "none",
                "document_uuids": [],
                "document_titles": [],
                "accessible_document_count": 0,
                "selected_document_count": 0,
                "retrieval_skipped": True,
                "conversation_memory": True,
            },
            "firewall": {
                "requested_provider_type": "ollama",
                "effective_provider_type": "ollama",
                "forced_local": True,
                "allowed_count": 0,
                "blocked_count": 0,
                "redacted_count": 0,
                "policy_actions": [],
            },
        }

    @classmethod
    def _get_or_create_conversation(
        cls,
        user,
        message,
        conversation_uuid=None,
        department_uuid=None,
        scope=Document.Scope.INTERNAL,
    ):
        if conversation_uuid:
            return AIConversation.objects.get(
                uuid=conversation_uuid,
                company=user.company,
                user=user,
                scope=scope,
                is_deleted=False,
            )

        department = None

        if department_uuid:
            from apps.workspaces.models import Department

            department = Department.objects.filter(
                uuid=department_uuid,
                company=user.company,
                is_deleted=False,
            ).first()

        title = message[:80]

        return AIConversation.objects.create(
            company=user.company,
            user=user,
            scope=scope,
            department=department,
            title=title,
        )

    @staticmethod
    def _build_sources(retrieved_chunks):
        sources = []

        for chunk in retrieved_chunks:
            sources.append(
                {
                    "chunk_uuid": chunk.get("chunk_uuid"),
                    "document_uuid": chunk.get("document_uuid"),
                    "governance_revision": chunk.get("governance_revision"),
                    "document_title": chunk.get("document_title"),
                    "document_filename": chunk.get("document_filename"),
                    "document_mime_type": chunk.get("document_mime_type"),
                    "department_uuid": chunk.get("department_uuid"),
                    "department_name": chunk.get("department_name"),
                    "page_number": chunk.get("page_number"),
                    "chunk_index": chunk.get("chunk_index"),
                    "score": chunk.get("score"),
                    "distance": chunk.get("distance"),
                    "content_preview": chunk.get("content", "")[:500],
                    "metadata": chunk.get("metadata", {}),
                }
            )

        return sources
