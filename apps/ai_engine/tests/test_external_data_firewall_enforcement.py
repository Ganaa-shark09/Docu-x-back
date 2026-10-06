from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.test import SimpleTestCase

from apps.ai_engine.services.external_chat_service import (
    ExternalChatService,
)
from apps.ai_engine.services.external_document_summary_service import (
    ExternalDocumentSummaryService,
)
from apps.documents.models import Document


class ExternalChatFirewallTests(SimpleTestCase):
    @patch.object(
        ExternalChatService,
        "_response",
        return_value={"ok": True},
    )
    @patch.object(
        ExternalChatService,
        "_build_sources",
        return_value=[],
    )
    @patch("apps.ai_engine.services.external_chat_service." "AIMessage.objects.create")
    @patch(
        "apps.ai_engine.services.external_chat_service."
        "LLMGatewayService.generate_answer"
    )
    @patch(
        "apps.ai_engine.services.external_chat_service."
        "AIContextPolicyEnforcementService.enforce"
    )
    @patch(
        "apps.ai_engine.services.external_chat_service."
        "LLMProviderConfigService."
        "get_default_config_for_company"
    )
    def test_chat_uses_sanitized_context(
        self,
        get_provider,
        enforce,
        generate_answer,
        create_message,
        build_sources,
        response,
    ):
        requested_provider = SimpleNamespace(provider_type="openai")

        effective_provider = SimpleNamespace(provider_type="ollama")

        document_uuid = str(uuid4())

        safe_chunks = [
            {
                "document_uuid": document_uuid,
                "document_title": "Safe document",
                "content": "[REDACTED_EMAIL]",
                "chunk_index": 0,
            }
        ]

        get_provider.return_value = requested_provider

        enforce.return_value = {
            "chunks": safe_chunks,
            "provider_config": effective_provider,
            "requested_provider_type": "openai",
            "effective_provider_type": "ollama",
            "forced_local": True,
            "allowed_count": 1,
            "blocked_count": 0,
            "redacted_count": 1,
            "policy_actions": [],
        }

        generate_answer.return_value = {
            "answer": "Safe answer",
            "provider": "ollama",
            "model": "test",
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
            "metadata": {},
        }

        create_message.return_value = SimpleNamespace(uuid=uuid4())

        ExternalChatService._answer_with_llm(
            user=SimpleNamespace(company=SimpleNamespace()),
            conversation=SimpleNamespace(save=lambda **kwargs: None),
            user_message=SimpleNamespace(uuid=uuid4()),
            message="Explain this document",
            intent="document",
            document_task="qa",
            document_chunks=[
                {
                    "document_uuid": document_uuid,
                    "document_title": "Unsafe",
                    "content": "person@example.com",
                    "chunk_index": 0,
                }
            ],
            web_results=[],
            web_warnings=[],
            processing_documents=[],
            failed_documents=[],
            attached_document_uuids=[document_uuid],
        )

        kwargs = generate_answer.call_args.kwargs

        self.assertEqual(
            kwargs["retrieved_chunks"],
            safe_chunks,
        )

        self.assertIs(
            kwargs["provider_config"],
            effective_provider,
        )

        source_kwargs = build_sources.call_args.kwargs

        self.assertEqual(
            source_kwargs["document_chunks"],
            safe_chunks,
        )

    @patch.object(
        ExternalChatService,
        "_response",
        return_value={"ok": True},
    )
    @patch.object(
        ExternalChatService,
        "_build_sources",
        return_value=[],
    )
    @patch("apps.ai_engine.services.external_chat_service." "AIMessage.objects.create")
    @patch(
        "apps.ai_engine.services.external_chat_service."
        "LLMGatewayService.generate_answer"
    )
    @patch(
        "apps.ai_engine.services.external_chat_service."
        "AIContextPolicyEnforcementService.enforce"
    )
    @patch(
        "apps.ai_engine.services.external_chat_service."
        "LLMProviderConfigService."
        "get_default_config_for_company"
    )
    def test_chat_does_not_call_llm_when_blocked(
        self,
        get_provider,
        enforce,
        generate_answer,
        create_message,
        build_sources,
        response,
    ):
        get_provider.return_value = SimpleNamespace(provider_type="openai")

        enforce.return_value = {
            "chunks": [],
            "provider_config": None,
            "requested_provider_type": "openai",
            "effective_provider_type": "local",
            "forced_local": False,
            "allowed_count": 0,
            "blocked_count": 1,
            "redacted_count": 0,
            "policy_actions": [],
        }

        create_message.return_value = SimpleNamespace(uuid=uuid4())

        ExternalChatService._answer_with_llm(
            user=SimpleNamespace(company=SimpleNamespace()),
            conversation=SimpleNamespace(save=lambda **kwargs: None),
            user_message=SimpleNamespace(uuid=uuid4()),
            message="Explain this document",
            intent="document",
            document_task="qa",
            document_chunks=[
                {
                    "document_uuid": str(uuid4()),
                    "document_title": "Blocked",
                    "content": "secret",
                    "chunk_index": 0,
                }
            ],
            web_results=[],
            web_warnings=[],
            processing_documents=[],
            failed_documents=[],
            attached_document_uuids=[],
        )

        generate_answer.assert_not_called()

        created = create_message.call_args.kwargs

        self.assertEqual(
            created["model_provider"],
            "data_firewall",
        )


class ExternalSummaryFirewallTests(SimpleTestCase):
    @patch.object(
        ExternalDocumentSummaryService,
        "summarize_single_pass",
    )
    @patch.object(
        ExternalDocumentSummaryService,
        "should_use_single_pass",
        return_value=True,
    )
    @patch.object(
        ExternalDocumentSummaryService,
        "get_extracted_text",
        return_value="raw@example.com",
    )
    @patch(
        "apps.ai_engine.services."
        "external_document_summary_service."
        "AIContextPolicyEnforcementService.enforce"
    )
    @patch(
        "apps.ai_engine.services."
        "external_document_summary_service."
        "LLMProviderConfigService."
        "get_default_config_for_company"
    )
    @patch(
        "apps.ai_engine.services."
        "external_document_summary_service."
        "ExternalDocumentRetrievalService."
        "get_candidate_documents"
    )
    def test_summary_uses_sanitized_text(
        self,
        get_documents,
        get_provider,
        enforce,
        get_text,
        should_single_pass,
        summarize_single_pass,
    ):
        document_uuid = uuid4()

        document = SimpleNamespace(
            uuid=document_uuid,
            title="Confidential",
            sensitivity_label=(Document.Sensitivity.CONFIDENTIAL),
            is_ai_ready=True,
            status=Document.Status.READY,
        )

        effective_provider = SimpleNamespace(provider_type="openai")

        get_documents.return_value = [document]

        get_provider.return_value = SimpleNamespace(provider_type="openai")

        enforce.return_value = {
            "chunks": [
                {
                    "document_uuid": str(document_uuid),
                    "document_title": (document.title),
                    "content": ("[REDACTED_EMAIL]"),
                    "chunk_index": 0,
                }
            ],
            "provider_config": effective_provider,
            "requested_provider_type": "openai",
            "effective_provider_type": "openai",
            "forced_local": False,
            "allowed_count": 1,
            "blocked_count": 0,
            "redacted_count": 1,
            "policy_actions": [],
        }

        summarize_single_pass.return_value = {
            "answer": "Summary",
            "sources": [],
            "provider": "openai",
            "model": "test",
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
            "metadata": {},
        }

        result = ExternalDocumentSummaryService.summarize(
            user=SimpleNamespace(company=SimpleNamespace()),
            conversation=SimpleNamespace(),
            question="Summarize",
            explicit_document_uuids=[str(document_uuid)],
        )

        kwargs = summarize_single_pass.call_args.kwargs

        self.assertEqual(
            kwargs["full_text"],
            "[REDACTED_EMAIL]",
        )

        self.assertIs(
            kwargs["provider_config"],
            effective_provider,
        )

        self.assertEqual(
            result["metadata"]["firewall_enforcement"]["redacted_count"],
            1,
        )

    @patch.object(
        ExternalDocumentSummaryService,
        "summarize_single_pass",
    )
    @patch.object(
        ExternalDocumentSummaryService,
        "get_extracted_text",
        return_value="blocked secret",
    )
    @patch(
        "apps.ai_engine.services."
        "external_document_summary_service."
        "AIContextPolicyEnforcementService.enforce"
    )
    @patch(
        "apps.ai_engine.services."
        "external_document_summary_service."
        "LLMProviderConfigService."
        "get_default_config_for_company"
    )
    @patch(
        "apps.ai_engine.services."
        "external_document_summary_service."
        "ExternalDocumentRetrievalService."
        "get_candidate_documents"
    )
    def test_summary_stops_when_blocked(
        self,
        get_documents,
        get_provider,
        enforce,
        get_text,
        summarize_single_pass,
    ):
        document = SimpleNamespace(
            uuid=uuid4(),
            title="Restricted",
            sensitivity_label=(Document.Sensitivity.RESTRICTED),
            is_ai_ready=True,
            status=Document.Status.READY,
        )

        get_documents.return_value = [document]

        get_provider.return_value = SimpleNamespace(provider_type="openai")

        enforce.return_value = {
            "chunks": [],
            "provider_config": None,
            "requested_provider_type": "openai",
            "effective_provider_type": "local",
            "forced_local": False,
            "allowed_count": 0,
            "blocked_count": 1,
            "redacted_count": 0,
            "policy_actions": [],
        }

        result = ExternalDocumentSummaryService.summarize(
            user=SimpleNamespace(company=SimpleNamespace()),
            conversation=SimpleNamespace(),
            question="Summarize",
            explicit_document_uuids=[str(document.uuid)],
        )

        summarize_single_pass.assert_not_called()

        self.assertEqual(
            result["provider"],
            "data_firewall",
        )

        self.assertFalse(result["metadata"]["complete"])
