from django.db import transaction

from apps.ai_engine.models import AIConversation, AIMessage
from apps.ai_engine.services.llm_gateway_service import LLMGatewayService
from apps.ai_engine.services.prompt_builder_service import PromptBuilderService
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
            query=message,
            department_uuid=department_uuid,
            document_type=document_type,
            sensitivity_label=sensitivity_label,
            top_k=top_k,
        )

        prompt = PromptBuilderService.build_rag_prompt(
            user_question=message,
            retrieved_chunks=retrieved_chunks,
        )

        llm_result = LLMGatewayService.generate_answer(
            question=message,
            retrieved_chunks=retrieved_chunks,
            prompt=prompt,
        )

        assistant_message = AIMessage.objects.create(
            conversation=conversation,
            role=AIMessage.Role.ASSISTANT,
            content=llm_result["answer"],
            retrieved_sources=cls._build_sources(retrieved_chunks),
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
            },
        )

        conversation.save(update_fields=["updated_at"])

        return {
            "conversation": conversation,
            "user_message": user_message,
            "assistant_message": assistant_message,
            "sources": assistant_message.retrieved_sources,
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
