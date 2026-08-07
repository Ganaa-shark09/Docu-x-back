from django.db import transaction

from apps.ai_engine.models import AIConversation, AIConversationDocument, AIMessage
from apps.ai_engine.services.external_document_retrieval_service import (
    ExternalDocumentRetrievalService,
)
from apps.ai_engine.services.external_intent_router_service import (
    ExternalIntentRouterService,
)
from apps.ai_engine.services.llm_gateway_service import LLMGatewayService
from apps.ai_engine.services.llm_provider_config_service import LLMProviderConfigService
from apps.ai_engine.services.web_search_provider_service import WebSearchProviderService
from apps.documents.models import Document
from apps.ai_engine.services.external_extractive_qa_service import (
    ExternalExtractiveQAService,
)


class ExternalChatService:
    @classmethod
    @transaction.atomic
    def chat(
        cls,
        user,
        message,
        conversation_uuid=None,
        top_k=8,
        use_web=True,
        document_uuids=None,
    ):
        conversation = cls._get_or_create_conversation(
            user=user,
            message=message,
            conversation_uuid=conversation_uuid,
        )

        user_message = AIMessage.objects.create(
            conversation=conversation,
            role=AIMessage.Role.USER,
            content=message,
        )

        attached_document_uuids = cls._get_attached_document_uuids(conversation)

        route = ExternalIntentRouterService.route(
            message=message,
            has_attached_documents=bool(attached_document_uuids),
            explicit_document_uuids=document_uuids,
            use_web=use_web,
        )

        if route["intent"] == "calculator":
            return cls._answer_calculator(
                conversation=conversation,
                user_message=user_message,
                message=message,
                route=route,
                attached_document_uuids=attached_document_uuids,
            )

        document_retrieval = {
            "chunks": [],
            "processing_documents": [],
            "failed_documents": [],
            "has_sufficient_context": False,
            "all_ready_count": 0,
        }

        if route["should_use_documents"]:
            document_retrieval = ExternalDocumentRetrievalService.retrieve(
                user=user,
                conversation=conversation,
                query=message,
                explicit_document_uuids=document_uuids,
                top_k=top_k,
            )

            if route["intent"] == "document_question":
                if document_retrieval["has_sufficient_context"]:
                    extractive_result = ExternalExtractiveQAService.try_answer(
                        user=user,
                        conversation=conversation,
                        query=message,
                        explicit_document_uuids=document_uuids,
                    )

                    if extractive_result:
                        return cls._answer_extractive(
                            conversation=conversation,
                            user_message=user_message,
                            extractive_result=extractive_result,
                            attached_document_uuids=attached_document_uuids,
                        )

                    return cls._answer_with_llm(
                        user=user,
                        conversation=conversation,
                        user_message=user_message,
                        message=message,
                        intent=route["intent"],
                        document_chunks=document_retrieval["chunks"],
                        web_results=[],
                        web_warnings=[],
                        processing_documents=document_retrieval["processing_documents"],
                        failed_documents=document_retrieval["failed_documents"],
                        attached_document_uuids=attached_document_uuids,
                    )

                if use_web:
                    web_response = WebSearchProviderService.search_with_metadata(
                        query=message,
                        max_results=5,
                    )

                    if web_response.get("results"):
                        return cls._answer_with_llm(
                            user=user,
                            conversation=conversation,
                            user_message=user_message,
                            message=message,
                            intent="document_fallback_web",
                            document_chunks=[],
                            web_results=web_response.get("results", []),
                            web_warnings=web_response.get("warnings", []),
                            processing_documents=document_retrieval[
                                "processing_documents"
                            ],
                            failed_documents=document_retrieval["failed_documents"],
                            attached_document_uuids=attached_document_uuids,
                        )

                return cls._answer_document_context_unavailable(
                    conversation=conversation,
                    user_message=user_message,
                    document_retrieval=document_retrieval,
                    attached_document_uuids=attached_document_uuids,
                )

        # Hybrid behavior:
        # Attached docs are checked first. If they provide context, use them.
        # If not, fallback to web/general without being blocked by documents.
        if route["intent"] == "hybrid" and document_retrieval["has_sufficient_context"]:
            extractive_result = ExternalExtractiveQAService.try_answer(
                user=user,
                conversation=conversation,
                query=message,
                explicit_document_uuids=document_uuids,
            )

            if extractive_result:
                return cls._answer_extractive(
                    conversation=conversation,
                    user_message=user_message,
                    extractive_result=extractive_result,
                    attached_document_uuids=attached_document_uuids,
                )

            return cls._answer_with_llm(
                user=user,
                conversation=conversation,
                user_message=user_message,
                message=message,
                intent="document_question",
                document_chunks=document_retrieval["chunks"],
                web_results=[],
                web_warnings=[],
                processing_documents=document_retrieval["processing_documents"],
                failed_documents=document_retrieval["failed_documents"],
                attached_document_uuids=attached_document_uuids,
            )

        web_results = []
        web_warnings = []

        if route["should_use_web"]:
            web_response = WebSearchProviderService.search_with_metadata(
                query=message,
                max_results=5,
            )
            web_results = web_response["results"]
            web_warnings = web_response["warnings"]

        return cls._answer_with_llm(
            user=user,
            conversation=conversation,
            user_message=user_message,
            message=message,
            intent=route["intent"],
            document_chunks=[],
            web_results=web_results,
            web_warnings=web_warnings,
            processing_documents=document_retrieval["processing_documents"],
            failed_documents=document_retrieval["failed_documents"],
            attached_document_uuids=attached_document_uuids,
        )

    @classmethod
    def _answer_calculator(
        cls,
        conversation,
        user_message,
        message,
        route,
        attached_document_uuids,
    ):
        calculation = route["calculation"]

        assistant_message = AIMessage.objects.create(
            conversation=conversation,
            role=AIMessage.Role.ASSISTANT,
            content=calculation["answer"],
            retrieved_sources=[],
            model_provider="system",
            model_name="deterministic_calculator",
            metadata={
                "scope": Document.Scope.EXTERNAL,
                "intent": "calculator",
                "expression": calculation["expression"],
                "result": calculation["result"],
                "attached_document_uuids": [
                    str(item) for item in attached_document_uuids
                ],
                "user_message_uuid": str(user_message.uuid),
            },
        )

        conversation.save(update_fields=["updated_at"])

        return cls._response(
            conversation=conversation,
            user_message=user_message,
            assistant_message=assistant_message,
            sources=[],
            web_results=[],
            external_chunks=[],
            processing_documents=[],
            failed_documents=[],
            search_warnings=[],
            intent="calculator",
        )

    @classmethod
    def _answer_document_context_unavailable(
        cls,
        conversation,
        user_message,
        document_retrieval,
        attached_document_uuids,
    ):
        processing_documents = document_retrieval["processing_documents"]
        failed_documents = document_retrieval["failed_documents"]

        if processing_documents and not failed_documents:
            answer = (
                "Your uploaded document is still being processed. "
                "Please try again in a few seconds."
            )
        elif failed_documents and not document_retrieval["chunks"]:
            failed_titles = ", ".join(item["title"] for item in failed_documents)
            answer = (
                "I cannot answer from the uploaded file because processing failed for: "
                f"{failed_titles}. Please upload the file again or retry processing."
            )
        else:
            answer = (
                "I could not find enough usable content in the attached documents "
                "to answer this question."
            )

        sources = cls._build_sources(
            document_chunks=[],
            web_results=[],
            processing_documents=processing_documents,
            failed_documents=failed_documents,
            search_warnings=[],
        )

        assistant_message = AIMessage.objects.create(
            conversation=conversation,
            role=AIMessage.Role.ASSISTANT,
            content=answer,
            retrieved_sources=sources,
            model_provider="system",
            model_name="document_context_gate",
            metadata={
                "scope": Document.Scope.EXTERNAL,
                "intent": "document_question",
                "attached_document_uuids": [
                    str(item) for item in attached_document_uuids
                ],
                "processing_documents": processing_documents,
                "failed_documents": failed_documents,
                "user_message_uuid": str(user_message.uuid),
            },
        )

        conversation.save(update_fields=["updated_at"])

        return cls._response(
            conversation=conversation,
            user_message=user_message,
            assistant_message=assistant_message,
            sources=sources,
            web_results=[],
            external_chunks=[],
            processing_documents=processing_documents,
            failed_documents=failed_documents,
            search_warnings=[],
            intent="document_question",
        )

    @classmethod
    def _answer_extractive(
        cls,
        conversation,
        user_message,
        extractive_result,
        attached_document_uuids,
    ):
        sources = extractive_result.get("sources", [])

        assistant_message = AIMessage.objects.create(
            conversation=conversation,
            role=AIMessage.Role.ASSISTANT,
            content=extractive_result["answer"],
            retrieved_sources=sources,
            model_provider="system",
            model_name="extractive_document_qa",
            metadata={
                "scope": Document.Scope.EXTERNAL,
                "intent": "document_question",
                "extractive": True,
                "attached_document_uuids": [
                    str(item) for item in attached_document_uuids
                ],
                "user_message_uuid": str(user_message.uuid),
                "extractive_metadata": extractive_result.get("metadata", {}),
            },
        )

        conversation.save(update_fields=["updated_at"])

        return cls._response(
            conversation=conversation,
            user_message=user_message,
            assistant_message=assistant_message,
            sources=sources,
            web_results=[],
            external_chunks=[
                {
                    "content": source.get("content_preview", ""),
                    "document_uuid": source.get("document_uuid"),
                    "document_title": source.get("document_title"),
                    "score": source.get("score", 1.0),
                }
                for source in sources
            ],
            processing_documents=[],
            failed_documents=[],
            search_warnings=[],
            intent="document_question",
        )

    @classmethod
    def _answer_with_llm(
        cls,
        user,
        conversation,
        user_message,
        message,
        intent,
        document_chunks,
        web_results,
        web_warnings,
        processing_documents,
        failed_documents,
        attached_document_uuids,
    ):
        prompt = cls._build_prompt(
            question=message,
            intent=intent,
            document_chunks=document_chunks,
            web_results=web_results,
            web_warnings=web_warnings,
        )

        provider_config = LLMProviderConfigService.get_default_config_for_company(
            user.company
        )

        llm_result = LLMGatewayService.generate_answer(
            question=message,
            retrieved_chunks=document_chunks,
            prompt=prompt,
            provider_config=provider_config,
            allow_general_answer=True,
        )

        sources = cls._build_sources(
            document_chunks=document_chunks,
            web_results=web_results,
            processing_documents=processing_documents,
            failed_documents=failed_documents,
            search_warnings=web_warnings,
        )

        assistant_message = AIMessage.objects.create(
            conversation=conversation,
            role=AIMessage.Role.ASSISTANT,
            content=llm_result["answer"],
            retrieved_sources=sources,
            model_provider=llm_result["provider"],
            model_name=llm_result["model"],
            prompt_tokens=llm_result["prompt_tokens"],
            completion_tokens=llm_result["completion_tokens"],
            total_tokens=llm_result["total_tokens"],
            metadata={
                "scope": Document.Scope.EXTERNAL,
                "intent": intent,
                "attached_document_uuids": [
                    str(item) for item in attached_document_uuids
                ],
                "web_result_count": len(web_results),
                "external_chunk_count": len(document_chunks),
                "search_warnings": web_warnings,
                "user_message_uuid": str(user_message.uuid),
            },
        )

        conversation.save(update_fields=["updated_at"])

        return cls._response(
            conversation=conversation,
            user_message=user_message,
            assistant_message=assistant_message,
            sources=sources,
            web_results=web_results,
            external_chunks=document_chunks,
            processing_documents=processing_documents,
            failed_documents=failed_documents,
            search_warnings=web_warnings,
            intent=intent,
        )

    @staticmethod
    def _build_prompt(
        question,
        intent,
        document_chunks,
        web_results,
        web_warnings,
    ):
        context_parts = []

        if document_chunks:
            context_parts.append("ATTACHED DOCUMENT CONTEXT:")

            for index, chunk in enumerate(document_chunks, start=1):
                context_parts.append(
                    f"[Document Source {index}]\n"
                    f"Document: {chunk.get('document_title')}\n"
                    f"Content:\n{chunk.get('content')}"
                )

        if web_results:
            context_parts.append("WEB CONTEXT:")

            for index, result in enumerate(web_results, start=1):
                context_parts.append(
                    f"[Web Source {index}]\n"
                    f"Title: {result.get('title')}\n"
                    f"Snippet: {result.get('snippet')}\n"
                    f"URL: {result.get('url')}"
                )

        context = "\n\n---\n\n".join(context_parts)

        if context:
            return (
                "You are DocuX External AI.\n"
                "Follow these rules strictly:\n"
                "1. Give the direct answer first.\n"
                "2. Use attached document context for document questions.\n"
                "3. Use web context for public/current questions.\n"
                "4. Do not invent sources.\n"
                "5. Do not mention unavailable search providers as factual sources.\n"
                "6. If context is insufficient, say you are not sure.\n"
                "7. For screenshots/images, OCR-visible text is more reliable than visual guesses.\n"
                "8. If visual summary conflicts with OCR text, prefer OCR text.\n"
                "9. When summarizing screenshots, mention visible page title, modal/dialog, sidebar, buttons, messages, and important text if present.\n"
                "10. If the user asks for a prompt, message, field, turn, or instruction, extract the most complete matching text from context.\n"
                "11. Do not stop after the first sentence if the source clearly contains a longer prompt or instruction.\n"
                "12. Do not confuse document context with unrelated web context.\n"
                "13. If intent is document_fallback_web, it means attached documents did not contain enough answer context, so answer from web context and do not claim the answer came from the document.\n\n"
                f"Intent: {intent}\n\n"
                f"Context:\n{context}\n\n"
                f"Question:\n{question}\n\n"
                "Answer:"
            )

        if web_warnings and intent in ["web_question", "general_web"]:
            return (
                "You are DocuX External AI.\n"
                "The web search provider did not return usable results.\n"
                "Answer only if this is general stable knowledge. "
                "If the question requires current facts, say you could not verify it.\n\n"
                f"Question:\n{question}\n\n"
                "Answer:"
            )

        return (
            "You are DocuX External AI.\n"
            "Answer the user's question clearly and directly.\n"
            "If the question needs current information and no web context is available, say you cannot verify it.\n\n"
            f"Question:\n{question}\n\n"
            "Answer:"
        )

    @classmethod
    def _get_or_create_conversation(cls, user, message, conversation_uuid=None):
        if conversation_uuid:
            return AIConversation.objects.get(
                uuid=conversation_uuid,
                company=user.company,
                user=user,
                scope=AIConversation.Scope.EXTERNAL,
                is_deleted=False,
            )

        return AIConversation.objects.create(
            company=user.company,
            user=user,
            scope=AIConversation.Scope.EXTERNAL,
            title=message[:80],
        )

    @staticmethod
    def _get_attached_document_uuids(conversation):
        return list(
            AIConversationDocument.objects.filter(
                company=conversation.company,
                conversation=conversation,
                is_deleted=False,
                document__is_deleted=False,
                document__scope=Document.Scope.EXTERNAL,
            ).values_list("document__uuid", flat=True)
        )

    @staticmethod
    def _build_sources(
        document_chunks,
        web_results,
        processing_documents,
        failed_documents,
        search_warnings,
    ):
        sources = []

        for chunk in document_chunks:
            sources.append(
                {
                    "source_type": "external_document",
                    "chunk_uuid": chunk.get("chunk_uuid"),
                    "document_uuid": chunk.get("document_uuid"),
                    "document_title": chunk.get("document_title"),
                    "chunk_index": chunk.get("chunk_index"),
                    "score": chunk.get("score"),
                    "content_preview": chunk.get("content", "")[:700],
                }
            )

        for result in web_results:
            sources.append(
                {
                    "source_type": "web",
                    "title": result.get("title"),
                    "snippet": result.get("snippet"),
                    "url": result.get("url"),
                    "source": result.get("source"),
                }
            )

        for document in processing_documents or []:
            sources.append(
                {
                    "source_type": "processing_document",
                    "document_uuid": document.get("uuid"),
                    "document_title": document.get("title"),
                    "status": document.get("status"),
                    "processing_error": document.get("processing_error"),
                }
            )

        for document in failed_documents or []:
            sources.append(
                {
                    "source_type": "failed_document",
                    "document_uuid": document.get("uuid"),
                    "document_title": document.get("title"),
                    "status": document.get("status"),
                    "processing_error": document.get("processing_error"),
                }
            )

        return sources

    @staticmethod
    def _response(
        conversation,
        user_message,
        assistant_message,
        sources,
        web_results,
        external_chunks,
        processing_documents,
        failed_documents,
        search_warnings,
        intent,
    ):
        return {
            "conversation": conversation,
            "user_message": user_message,
            "assistant_message": assistant_message,
            "sources": sources,
            "web_results": web_results,
            "external_chunks": external_chunks,
            "processing_documents": processing_documents,
            "failed_documents": failed_documents,
            "search_warnings": search_warnings,
            "intent": intent,
        }
