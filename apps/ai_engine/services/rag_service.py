from django.db import transaction

from apps.ai_engine.models import AIConversation, AIMessage
from apps.ai_engine.services.llm_gateway_service import LLMGatewayService
from apps.ai_engine.services.llm_provider_config_service import LLMProviderConfigService
from apps.ai_engine.services.prompt_builder_service import PromptBuilderService
from apps.audit.services import AuditLogService
from apps.data_firewall.services import AIContextPolicyEnforcementService
from apps.vector_search.services import VectorSearchService


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
        top_k=5,
        ip_address=None,
        user_agent="",
    ):
        conversation = cls._get_or_create_conversation(
            user=user,
            message=message,
            conversation_uuid=conversation_uuid,
            department_uuid=department_uuid,
        )

        user_message = AIMessage.objects.create(
            conversation=conversation,
            role=AIMessage.Role.USER,
            content=message,
        )

        retrieved_chunks = VectorSearchService.search(
            company=user.company,
            user=user,
            query=message,
            department_uuid=department_uuid,
            document_type=document_type,
            sensitivity_label=sensitivity_label,
            top_k=top_k,
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

        prompt = PromptBuilderService.build_rag_prompt(
            user_question=message,
            retrieved_chunks=safe_chunks,
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
    def _get_or_create_conversation(
        cls,
        user,
        message,
        conversation_uuid=None,
        department_uuid=None,
    ):
        if conversation_uuid:
            return AIConversation.objects.get(
                uuid=conversation_uuid,
                company=user.company,
                user=user,
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
                    "document_title": chunk.get("document_title"),
                    "department_uuid": chunk.get("department_uuid"),
                    "department_name": chunk.get("department_name"),
                    "chunk_index": chunk.get("chunk_index"),
                    "score": chunk.get("score"),
                    "distance": chunk.get("distance"),
                    "content_preview": chunk.get("content", "")[:500],
                    "metadata": chunk.get("metadata", {}),
                }
            )

        return sources
