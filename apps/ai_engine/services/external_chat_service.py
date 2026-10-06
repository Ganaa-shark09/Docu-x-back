import os

from django.db import transaction

from apps.ai_engine.models import AIConversation, AIConversationDocument, AIMessage
from apps.ai_engine.services.calculator_tool_service import CalculatorToolService
from apps.ai_engine.services.external_ai_model_policy import ExternalAIModelPolicy
from apps.ai_engine.services.external_document_exact_answer_service import (
    ExternalDocumentExactAnswerService,
)
from apps.ai_engine.services.external_document_retrieval_service import (
    ExternalDocumentRetrievalService,
)
from apps.ai_engine.services.external_document_summary_service import (
    ExternalDocumentSummaryService,
)
from apps.ai_engine.services.external_extractive_qa_service import (
    ExternalExtractiveQAService,
)
from apps.ai_engine.services.external_general_answer_service import (
    ExternalGeneralAnswerService,
)
from apps.ai_engine.services.external_semantic_orchestrator_service import (
    ExternalSemanticOrchestratorService,
)
from apps.ai_engine.services.external_web_plan_service import (
    ExternalWebPlanService,
)
from apps.ai_engine.services.llm_gateway_service import LLMGatewayService
from apps.ai_engine.services.llm_provider_config_service import LLMProviderConfigService
from apps.data_firewall.services import AIContextPolicyEnforcementService
from apps.ai_engine.services.external_web_search_service import (
    ExternalWebSearchService,
)
from apps.documents.models import Document


class ExternalChatService:
    @classmethod
    def _answer_semantic_fast(
        cls,
        conversation,
        user_message,
        plan,
        attached_document_uuids,
    ):
        answer = (plan.get("fast_answer") or "").strip()
        if not answer:
            return None

        assistant_message = AIMessage.objects.create(
            conversation=conversation,
            role=AIMessage.Role.ASSISTANT,
            content=answer,
            retrieved_sources=[],
            model_provider=plan.get("provider", "ollama"),
            model_name=plan.get("model", ExternalAIModelPolicy.direct_fast_model()),
            prompt_tokens=plan.get("prompt_tokens", 0),
            completion_tokens=plan.get("completion_tokens", 0),
            total_tokens=plan.get("total_tokens", 0),
            metadata={
                "scope": Document.Scope.EXTERNAL,
                "intent": "general_question",
                "semantic_action": "direct",
                "answer_tier": "fast",
                "answer_profile": plan.get("answer_profile", "general"),
                "reasoning_mode": "none",
                "routing_model": plan.get("model"),
                "fast_answer_reused": True,
                "attached_document_uuids": [
                    str(item) for item in attached_document_uuids
                ],
                "user_message_uuid": str(user_message.uuid),
                "orchestrator_metadata": plan.get("metadata", {}),
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
            intent="general_question",
        )

    @classmethod
    def _answer_semantic_direct(
        cls,
        conversation,
        user_message,
        plan,
        attached_document_uuids,
    ):
        action = plan["action"]

        assistant_message = AIMessage.objects.create(
            conversation=conversation,
            role=AIMessage.Role.ASSISTANT,
            content=plan.get("clarification")
            or "Could you provide a little more detail?",
            retrieved_sources=[],
            model_provider=plan.get(
                "provider",
                "ollama",
            ),
            model_name=plan.get(
                "model",
                "qwen3:8b",
            ),
            prompt_tokens=plan.get(
                "prompt_tokens",
                0,
            ),
            completion_tokens=plan.get(
                "completion_tokens",
                0,
            ),
            total_tokens=plan.get(
                "total_tokens",
                0,
            ),
            metadata={
                "scope": Document.Scope.EXTERNAL,
                "intent": action,
                "semantic_action": action,
                "semantic_orchestrator": True,
                "attached_document_uuids": [
                    str(item) for item in attached_document_uuids
                ],
                "user_message_uuid": str(user_message.uuid),
                "orchestrator_metadata": plan.get(
                    "metadata",
                    {},
                ),
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
            intent=action,
        )

    @classmethod
    def _answer_web_context_unavailable(
        cls,
        conversation,
        user_message,
        web_warnings,
        attached_document_uuids,
    ):
        answer = (
            "I couldn't verify this request with reliable current web evidence. "
            "I won't guess. Please try again, or ask me to check a specific "
            "publisher or article."
        )

        sources = cls._build_sources(
            document_chunks=[],
            web_results=[],
            processing_documents=[],
            failed_documents=[],
            search_warnings=web_warnings,
        )

        assistant_message = AIMessage.objects.create(
            conversation=conversation,
            role=AIMessage.Role.ASSISTANT,
            content=answer,
            retrieved_sources=sources,
            model_provider="system",
            model_name="web_context_gate",
            metadata={
                "scope": Document.Scope.EXTERNAL,
                "intent": "web",
                "semantic_action": "web",
                "search_warnings": web_warnings,
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
            sources=sources,
            web_results=[],
            external_chunks=[],
            processing_documents=[],
            failed_documents=[],
            search_warnings=web_warnings,
            intent="web",
        )

    @classmethod
    def chat(
        cls,
        user,
        message,
        conversation_uuid=None,
        top_k=8,
        use_web=True,
        document_uuids=None,
        routing_plan=None,
        document_descriptors=None,
        document_scope_mode="none",
    ):
        with transaction.atomic():
            conversation = cls._get_or_create_conversation(
                user=user,
                message=message,
                conversation_uuid=conversation_uuid,
            )

            user_message = AIMessage.objects.create(
                conversation=conversation,
                role=AIMessage.Role.USER,
                content=message,
                metadata={
                    "scope": Document.Scope.EXTERNAL,
                    "requested_document_uuids": [
                        str(item) for item in (document_uuids or [])
                    ],
                    "document_scope_mode": document_scope_mode,
                },
            )

        attached_document_uuids = cls._get_attached_document_uuids(conversation)

        # ---------------------------------------------------------
        # Deterministic tool: calculator
        # ---------------------------------------------------------
        calculation = CalculatorToolService.calculate(message)

        if calculation:
            return cls._answer_calculator(
                conversation=conversation,
                user_message=user_message,
                message=message,
                route={
                    "intent": "calculator",
                    "calculation": calculation,
                },
                attached_document_uuids=attached_document_uuids,
            )

        # ---------------------------------------------------------
        # Semantic planning + possible direct answer
        # ---------------------------------------------------------
        plan = routing_plan or ExternalSemanticOrchestratorService.plan_resilient(
            message=message,
            conversation=conversation,
            current_user_message_uuid=user_message.uuid,
            has_attached_documents=bool(attached_document_uuids),
            has_explicit_documents=(document_scope_mode == "selected"),
            use_web=use_web,
            document_descriptors=document_descriptors,
            document_scope_mode=document_scope_mode,
        )

        web_search_plan = ExternalWebPlanService.from_routing_plan(
            message=message,
            routing_plan=plan,
            use_web=use_web,
        )

        plan = dict(plan)

        plan["web_search_plan"] = web_search_plan

        action = plan["action"]
        document_task = plan.get("document_task", "none")
        cls._persist_routing_decision(user_message, plan)

        # The orchestrator may return the final answer only for bounded fast
        # direct requests. Quality requests are classification-only and always
        # escalate to the stronger answer model.
        if action == "direct":
            answer_tier = plan.get("answer_tier", "quality")

            # For bounded fast requests, the orchestrator already generated the
            # complete answer. Reuse it so a simple message costs one 4B call
            # instead of a classifier call followed by another 4B call.
            if answer_tier == "fast":
                fast_response = cls._answer_semantic_fast(
                    conversation=conversation,
                    user_message=user_message,
                    plan=plan,
                    attached_document_uuids=attached_document_uuids,
                )
                if fast_response is not None:
                    return fast_response

            answer_profile = plan.get("answer_profile", "general")
            answer_model = ExternalAIModelPolicy.direct_model(
                answer_tier=answer_tier,
                answer_profile=answer_profile,
            )
            return cls._answer_external_general(
                conversation=conversation,
                user_message=user_message,
                message=message,
                intent="general_question",
                attached_document_uuids=attached_document_uuids,
                model=answer_model,
                routing_plan=plan,
            )

        if action == "clarify":
            return cls._answer_semantic_direct(
                conversation=conversation,
                user_message=user_message,
                plan=plan,
                attached_document_uuids=attached_document_uuids,
            )

        # ---------------------------------------------------------
        # Tool execution state
        # ---------------------------------------------------------
        document_retrieval = {
            "chunks": [],
            "processing_documents": [],
            "failed_documents": [],
            "has_sufficient_context": False,
            "all_ready_count": 0,
        }

        # ---------------------------------------------------------
        # Document retrieval
        # ---------------------------------------------------------
        if action in {
            "document",
            "document_web",
        }:
            document_query = plan.get("document_query") or message

            document_retrieval = ExternalDocumentRetrievalService.retrieve(
                user=user,
                conversation=conversation,
                query=document_query,
                explicit_document_uuids=document_uuids,
                top_k=top_k,
                document_required=True,
            )

            # Positional/structural lookups (for example, "14th record")
            # must be exact and one-based. Do not ask the LLM to count through
            # long context when source structure can answer deterministically.
            if (
                action == "document"
                and document_task in {"qa", "extract"}
                and document_retrieval["has_sufficient_context"]
            ):
                exact_result = ExternalDocumentExactAnswerService.try_answer(
                    user=user,
                    conversation=conversation,
                    query=message,
                    explicit_document_uuids=document_uuids,
                )

                if exact_result:
                    return cls._answer_exact_document(
                        conversation=conversation,
                        user_message=user_message,
                        exact_result=exact_result,
                        attached_document_uuids=attached_document_uuids,
                    )

            # Whole-document summaries do not use relevance-ranked top-k
            # chunks. They process every part of every target document and
            # perform hierarchical synthesis when the source is long.
            if (
                action == "document"
                and document_task == "summarize"
                and document_retrieval["has_sufficient_context"]
            ):
                summary_result = ExternalDocumentSummaryService.summarize(
                    user=user,
                    conversation=conversation,
                    question=message,
                    explicit_document_uuids=document_uuids,
                )

                if summary_result:
                    return cls._answer_document_summary(
                        conversation=conversation,
                        user_message=user_message,
                        summary_result=summary_result,
                        attached_document_uuids=attached_document_uuids,
                    )

            # For pure document extraction, keep the existing deterministic
            # turn/prompt extraction path when it can answer accurately.
            if (
                action == "document"
                and document_task == "extract"
                and document_retrieval["has_sufficient_context"]
            ):
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

            if (
                action == "document"
                and not document_retrieval["has_sufficient_context"]
            ):
                return cls._answer_document_context_unavailable(
                    conversation=conversation,
                    user_message=user_message,
                    document_retrieval=document_retrieval,
                    attached_document_uuids=attached_document_uuids,
                )

        # ---------------------------------------------------------
        # Web retrieval
        # ---------------------------------------------------------
        web_results = []
        web_warnings = []

        if action in {
            "web",
            "document_web",
        }:
            web_query = plan.get("web_query") or message

            web_response = ExternalWebSearchService.search(
                fallback_query=web_query,
                search_plan=plan.get("web_search_plan") or {},
                max_results=5,
            )

            web_results = web_response.get(
                "results",
                [],
            )

            web_warnings = web_response.get(
                "warnings",
                [],
            )

        # ---------------------------------------------------------
        # Tool failure gate
        # ---------------------------------------------------------
        active_web_plan = plan.get("web_search_plan") or {}

        verified_web_results = [
            item for item in web_results if str(item.get("content") or "").strip()
        ]

        needs_verified_web = bool(active_web_plan.get("needs_web")) and action in {
            "web",
            "document_web",
        }

        if (action == "web" and not web_results) or (
            needs_verified_web and not verified_web_results
        ):
            if web_results and not verified_web_results:
                web_warnings = [
                    *web_warnings,
                    (
                        "Search results were found, but publisher "
                        "pages could not be verified."
                    ),
                ]

            return cls._answer_web_context_unavailable(
                conversation=conversation,
                user_message=user_message,
                web_warnings=web_warnings,
                attached_document_uuids=attached_document_uuids,
            )

        if (
            action == "document_web"
            and not document_retrieval["chunks"]
            and not web_results
        ):
            if (
                document_retrieval["processing_documents"]
                or document_retrieval["failed_documents"]
            ):
                return cls._answer_document_context_unavailable(
                    conversation=conversation,
                    user_message=user_message,
                    document_retrieval=document_retrieval,
                    attached_document_uuids=attached_document_uuids,
                )

            return cls._answer_web_context_unavailable(
                conversation=conversation,
                user_message=user_message,
                web_warnings=web_warnings,
                attached_document_uuids=attached_document_uuids,
            )

        # ---------------------------------------------------------
        # Evidence gathered -> final Qwen synthesis
        # ---------------------------------------------------------
        return cls._answer_with_llm(
            user=user,
            conversation=conversation,
            user_message=user_message,
            message=message,
            intent=action,
            document_task=document_task,
            document_chunks=document_retrieval["chunks"],
            web_results=web_results,
            web_warnings=web_warnings,
            processing_documents=document_retrieval["processing_documents"],
            failed_documents=document_retrieval["failed_documents"],
            attached_document_uuids=attached_document_uuids,
        )

    @classmethod
    def _answer_smalltalk(
        cls,
        conversation,
        user_message,
        attached_document_uuids,
    ):
        llm_result = ExternalGeneralAnswerService.generate(
            message=user_message.content,
            intent="smalltalk",
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
                "scope": Document.Scope.EXTERNAL,
                "intent": "smalltalk",
                "attached_document_uuids": [
                    str(item) for item in attached_document_uuids
                ],
                "user_message_uuid": str(user_message.uuid),
                "llm_metadata": llm_result.get("metadata", {}),
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
            intent="smalltalk",
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
    def _answer_exact_document(
        cls,
        conversation,
        user_message,
        exact_result,
        attached_document_uuids,
    ):
        sources = exact_result.get("sources", [])

        assistant_message = AIMessage.objects.create(
            conversation=conversation,
            role=AIMessage.Role.ASSISTANT,
            content=exact_result["answer"],
            retrieved_sources=sources,
            model_provider="system",
            model_name="exact_document_structure",
            metadata={
                "scope": Document.Scope.EXTERNAL,
                "intent": "document_question",
                "document_task": "qa",
                "exact_structural_answer": True,
                "attached_document_uuids": [
                    str(item) for item in attached_document_uuids
                ],
                "user_message_uuid": str(user_message.uuid),
                "exact_answer_metadata": exact_result.get("metadata", {}),
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
    def _answer_document_summary(
        cls,
        conversation,
        user_message,
        summary_result,
        attached_document_uuids,
    ):
        sources = summary_result.get("sources", [])

        assistant_message = AIMessage.objects.create(
            conversation=conversation,
            role=AIMessage.Role.ASSISTANT,
            content=summary_result["answer"],
            retrieved_sources=sources,
            model_provider=summary_result.get("provider", "system"),
            model_name=summary_result.get("model", "whole_document_summary"),
            prompt_tokens=summary_result.get("prompt_tokens", 0),
            completion_tokens=summary_result.get("completion_tokens", 0),
            total_tokens=summary_result.get("total_tokens", 0),
            metadata={
                "scope": Document.Scope.EXTERNAL,
                "intent": "document",
                "document_task": "summarize",
                "whole_document_summary": True,
                "attached_document_uuids": [
                    str(item) for item in attached_document_uuids
                ],
                "user_message_uuid": str(user_message.uuid),
                "summary_metadata": summary_result.get("metadata", {}),
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
            intent="document",
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
    def _answer_external_general(
        cls,
        conversation,
        user_message,
        message,
        intent,
        attached_document_uuids,
        model=None,
        routing_plan=None,
    ):
        llm_result = ExternalGeneralAnswerService.generate(
            message=message,
            intent=intent,
            conversation=conversation,
            current_user_message_uuid=user_message.uuid,
            model=model,
            reasoning_mode=(routing_plan or {}).get("reasoning_mode", "standard"),
            answer_profile=(routing_plan or {}).get("answer_profile", "general"),
            quality_requirements=(routing_plan or {}).get("quality_requirements", []),
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
                "scope": Document.Scope.EXTERNAL,
                "intent": intent,
                "attached_document_uuids": [
                    str(item) for item in attached_document_uuids
                ],
                "user_message_uuid": str(user_message.uuid),
                "llm_metadata": llm_result.get("metadata", {}),
                "semantic_action": (routing_plan or {}).get("action", "direct"),
                "answer_tier": (routing_plan or {}).get("answer_tier"),
                "task_domain": (routing_plan or {}).get("task_domain", "general"),
                "answer_profile": (routing_plan or {}).get("answer_profile", "general"),
                "quality_focus": (routing_plan or {}).get("quality_focus", []),
                "quality_requirements": (routing_plan or {}).get(
                    "quality_requirements", []
                ),
                "reasoning_mode": (routing_plan or {}).get("reasoning_mode"),
                "routing_model": (routing_plan or {}).get("model"),
                "orchestrator_metadata": (routing_plan or {}).get("metadata", {}),
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
            intent=intent,
        )

    @classmethod
    def _answer_with_llm(
        cls,
        user,
        conversation,
        user_message,
        message,
        intent,
        document_task,
        document_chunks,
        web_results,
        web_warnings,
        processing_documents,
        failed_documents,
        attached_document_uuids,
    ):
        requested_provider_config = (
            LLMProviderConfigService.get_default_config_for_company(user.company)
        )

        if document_chunks:
            enforcement_result = AIContextPolicyEnforcementService.enforce(
                user=user,
                provider_config=requested_provider_config,
                retrieved_chunks=document_chunks,
            )

            safe_document_chunks = enforcement_result["chunks"]
            effective_provider_config = enforcement_result["provider_config"]

        else:
            safe_document_chunks = []
            effective_provider_config = requested_provider_config

            enforcement_result = {
                "requested_provider_type": (
                    requested_provider_config.provider_type
                    if requested_provider_config
                    else "local"
                ),
                "effective_provider_type": (
                    requested_provider_config.provider_type
                    if requested_provider_config
                    else "local"
                ),
                "forced_local": False,
                "allowed_count": 0,
                "blocked_count": 0,
                "redacted_count": 0,
                "policy_actions": [],
            }

        prompt = cls._build_prompt(
            question=message,
            intent=intent,
            document_task=document_task,
            document_chunks=safe_document_chunks,
            web_results=web_results,
            web_warnings=web_warnings,
        )

        generation_options = {}
        allow_extractive_fallback = True

        if intent in {"document", "document_web"}:
            generation_options["model"] = (
                ExternalAIModelPolicy.document_reasoning_model()
            )
        elif intent == "web":
            generation_options["model"] = ExternalAIModelPolicy.web_answer_model()

        if document_task == "summarize":
            generation_options.update(
                {
                    "max_tokens": int(
                        os.getenv("OLLAMA_DOCUMENT_SUMMARY_MAX_TOKENS", "900")
                    ),
                    "timeout": int(
                        os.getenv("OLLAMA_DOCUMENT_SUMMARY_TIMEOUT_SECONDS", "360")
                    ),
                }
            )
            # Sentence-overlap fallback is not a valid whole-document summary.
            allow_extractive_fallback = False
        elif document_task == "analyze":
            generation_options.update(
                {
                    "max_tokens": int(
                        os.getenv("OLLAMA_DOCUMENT_ANALYSIS_MAX_TOKENS", "1000")
                    ),
                    "timeout": int(
                        os.getenv("OLLAMA_DOCUMENT_ANALYSIS_TIMEOUT_SECONDS", "300")
                    ),
                }
            )
            allow_extractive_fallback = False
        elif document_task == "qa":
            generation_options.update(
                {
                    "max_tokens": int(
                        os.getenv("OLLAMA_DOCUMENT_QA_MAX_TOKENS", "800")
                    ),
                    "timeout": int(
                        os.getenv("OLLAMA_DOCUMENT_QA_TIMEOUT_SECONDS", "240")
                    ),
                }
            )

        if (
            document_chunks
            and not safe_document_chunks
            and enforcement_result.get("blocked_count", 0) > 0
        ):
            llm_result = {
                "answer": (
                    "DocuX AI Data Firewall blocked the selected "
                    "document context because it is restricted or "
                    "requires approval before AI usage."
                ),
                "provider": "data_firewall",
                "model": "policy_enforcement",
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "metadata": {
                    "reason": ("all_external_document_context_blocked"),
                },
            }

        else:
            llm_result = LLMGatewayService.generate_answer(
                question=message,
                retrieved_chunks=safe_document_chunks,
                prompt=prompt,
                provider_config=effective_provider_config,
                allow_general_answer=True,
                generation_options=generation_options,
                allow_extractive_fallback=allow_extractive_fallback,
            )

        sources = cls._build_sources(
            document_chunks=safe_document_chunks,
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
                "external_chunk_count": len(safe_document_chunks),
                "document_task": document_task,
                "firewall_enforcement": {
                    "requested_provider_type": (
                        enforcement_result.get("requested_provider_type")
                    ),
                    "effective_provider_type": (
                        enforcement_result.get("effective_provider_type")
                    ),
                    "forced_local": enforcement_result.get(
                        "forced_local",
                        False,
                    ),
                    "allowed_count": enforcement_result.get(
                        "allowed_count",
                        0,
                    ),
                    "blocked_count": enforcement_result.get(
                        "blocked_count",
                        0,
                    ),
                    "redacted_count": enforcement_result.get(
                        "redacted_count",
                        0,
                    ),
                    "policy_actions": enforcement_result.get(
                        "policy_actions",
                        [],
                    ),
                },
                "search_warnings": web_warnings,
                "user_message_uuid": str(user_message.uuid),
                "llm_metadata": llm_result.get("metadata", {}),
            },
        )

        conversation.save(update_fields=["updated_at"])

        return cls._response(
            conversation=conversation,
            user_message=user_message,
            assistant_message=assistant_message,
            sources=sources,
            web_results=web_results,
            external_chunks=safe_document_chunks,
            processing_documents=processing_documents,
            failed_documents=failed_documents,
            search_warnings=web_warnings,
            intent=intent,
        )

    @staticmethod
    def _build_prompt(
        question,
        intent,
        document_task,
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
                content = result.get("content") or result.get("snippet") or ""

                published_at = result.get("published_at") or "unknown"

                context_parts.append(
                    f"[Web Source {index}]\n"
                    f"Title: {result.get('title')}\n"
                    f"Published: {published_at}\n"
                    f"URL: {result.get('url')}\n"
                    f"Evidence:\n{content}"
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
                "13. If intent is document_fallback_web, it means attached documents did not contain enough answer context, so answer from web context and do not claim the answer came from the document.\n"
                "14. For document summaries, synthesize the whole supplied document context into an overview and meaningful key points; explain the points briefly instead of returning one matching sentence or raw source line.\n"
                "15. For document analysis, reason across all supplied document context and clearly separate source facts from your interpretation.\n"
                "16. Match answer depth to the task. For detailed, explanatory, analytical, comparison, code, or multi-step requests, provide the complete answer and do not omit material points merely to be brief.\n"
                "17. For exact factual document questions, preserve names, dates, numbers, statuses, identifiers, and ordering exactly as supported by the source.\n"
                "18. For current/live public facts, make factual claims only when supported by WEB CONTEXT from fetched publisher pages. Search snippets are weaker evidence than fetched publisher text.\n"
                "19. If web sources disagree, state the disagreement instead of guessing.\n"
                "20. Never use prior assistant claims as evidence for a current fact.\n\n"
                f"Intent: {intent}\n"
                f"Document operation: {document_task}\n\n"
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

    @staticmethod
    def _recent_user_messages(
        conversation,
        current_user_message_uuid=None,
        limit=3,
    ):
        queryset = conversation.messages.filter(
            is_deleted=False,
            role=AIMessage.Role.USER,
        )

        if current_user_message_uuid:
            queryset = queryset.exclude(uuid=current_user_message_uuid)

        return list(
            queryset.order_by("-created_at").values_list("content", flat=True)[:limit]
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
                    "content_preview": (
                        chunk.get("source_preview") or chunk.get("content", "")
                    )[:700],
                }
            )

        for result in web_results:
            sources.append(
                {
                    "source_type": "web",
                    "title": result.get("title"),
                    "snippet": result.get("snippet"),
                    "content_preview": (
                        result.get("content") or result.get("snippet") or ""
                    )[:900],
                    "published_at": result.get("published_at"),
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
    def _persist_routing_decision(user_message, plan):
        metadata = dict(user_message.metadata or {})
        metadata["routing"] = {
            "action": plan.get("action"),
            "document_dependency": plan.get("document_dependency"),
            "document_task": plan.get("document_task"),
            "answer_tier": plan.get("answer_tier"),
            "model": plan.get("model"),
            "recovery": (plan.get("metadata") or {}).get("routing_recovery"),
            "web_search_plan": plan.get("web_search_plan") or {},
        }
        user_message.metadata = metadata
        user_message.save(update_fields=["metadata", "updated_at"])

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
