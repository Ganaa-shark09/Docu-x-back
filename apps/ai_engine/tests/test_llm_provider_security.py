from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory

from apps.accounts.models import User
from apps.ai_engine.permissions import IsLLMProviderAdmin
from apps.ai_engine.services.llm_gateway_service import (
    AzureOpenAILLMProvider,
)
from apps.ai_engine.services.llm_provider_security_service import (
    LLMProviderSecurityService,
)


class LLMProviderPermissionTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.permission = IsLLMProviderAdmin()

    def request_for(self, user):
        request = self.factory.get("/api/ai/llm-providers/")
        request.user = user
        return request

    def test_member_is_denied(self):
        user = SimpleNamespace(
            is_authenticated=True,
            is_superuser=False,
            role=User.Role.MEMBER,
        )

        self.assertFalse(
            self.permission.has_permission(
                self.request_for(user),
                None,
            )
        )

    def test_company_admin_is_allowed(self):
        user = SimpleNamespace(
            is_authenticated=True,
            is_superuser=False,
            role=User.Role.COMPANY_ADMIN,
        )

        self.assertTrue(
            self.permission.has_permission(
                self.request_for(user),
                None,
            )
        )

    def test_platform_admin_is_allowed(self):
        user = SimpleNamespace(
            is_authenticated=True,
            is_superuser=False,
            role=User.Role.PLATFORM_ADMIN,
        )

        self.assertTrue(
            self.permission.has_permission(
                self.request_for(user),
                None,
            )
        )


class LLMProviderEndpointSecurityTests(SimpleTestCase):
    def test_valid_azure_endpoint_is_allowed(self):
        endpoint = LLMProviderSecurityService.validate_endpoint(
            "azure_openai",
            "https://docux-prod.openai.azure.com/",
        )

        self.assertEqual(
            endpoint,
            "https://docux-prod.openai.azure.com",
        )

    def test_http_azure_endpoint_is_blocked(self):
        with self.assertRaises(ValueError):
            LLMProviderSecurityService.validate_endpoint(
                "azure_openai",
                "http://docux-prod.openai.azure.com",
            )

    def test_arbitrary_azure_hostname_is_blocked(self):
        with self.assertRaises(ValueError):
            LLMProviderSecurityService.validate_endpoint(
                "azure_openai",
                "https://attacker.example.com",
            )

    def test_raw_ip_azure_endpoint_is_blocked(self):
        with self.assertRaises(ValueError):
            LLMProviderSecurityService.validate_endpoint(
                "azure_openai",
                "https://127.0.0.1",
            )

    def test_openai_custom_endpoint_is_blocked(self):
        with self.assertRaises(ValueError):
            LLMProviderSecurityService.validate_endpoint(
                "openai",
                "https://attacker.example.com",
            )

    @patch("apps.ai_engine.services.llm_gateway_service.requests.post")
    def test_gateway_never_sends_key_to_unsafe_azure_endpoint(
        self,
        request_post,
    ):
        config = SimpleNamespace(
            endpoint_url="https://attacker.example.com",
            deployment_name="docux",
            api_version="2024-10-21",
            temperature=0.2,
            max_tokens=100,
            get_api_key=lambda: "super-secret-key",
        )

        result = AzureOpenAILLMProvider.generate_answer(
            question="test",
            retrieved_chunks=[],
            prompt="test",
            config=config,
        )

        request_post.assert_not_called()

        self.assertEqual(
            result["metadata"]["fallback_reason"],
            "unsafe_azure_openai_endpoint_blocked",
        )


class AzureConfigurationInputTests(SimpleTestCase):
    def test_invalid_deployment_name_is_blocked(self):
        with self.assertRaises(ValueError):
            LLMProviderSecurityService.validate_azure_deployment_name("../secret")

    def test_invalid_api_version_is_blocked(self):
        with self.assertRaises(ValueError):
            LLMProviderSecurityService.validate_azure_api_version(
                "2024-10-21&redirect=http://example.com"
            )
