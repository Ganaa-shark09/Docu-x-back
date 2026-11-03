import uuid
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework.exceptions import ValidationError
from apps.ai_engine.models import CompanyModelGrant, LLMProviderConfig
from apps.ai_engine.services.company_model_access_service import CompanyModelAccessService
from apps.tenants.models import Company


class CompanyModelGrantsTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.a = Company.objects.create(name="Grant Tenant A", slug="grant-tenant-a", allow_cloud_llm=True, allow_local_llm=True)
        self.b = Company.objects.create(name="Grant Tenant B", slug="grant-tenant-b", allow_cloud_llm=True, allow_local_llm=True)
        self.superuser = User.objects.create_superuser(username="model-platform", email="models@platform.test", password="supersecret")
        self.admin = User.objects.create_user(username="tenant-admin", password="pw", company=self.a, role="company_admin")
        self.member = User.objects.create_user(username="tenant-member", password="pw", company=self.a, role="member")
        self.other = User.objects.create_user(username="other-member", password="pw", company=self.b, role="member")
        self.config_a = LLMProviderConfig.objects.create(company=self.a, name="Ollama A", provider_type="ollama", is_active=True, model_name="qwen3:8b")
        self.config_b = LLMProviderConfig.objects.create(company=self.b, name="Ollama B", provider_type="ollama", is_active=True, model_name="qwen3:8b")
        self.grant_a = CompanyModelGrant.objects.create(company=self.a, provider_config=self.config_a, model_id="qwen3:8b", display_name="Qwen 3", is_default=True)
        self.grant_b = CompanyModelGrant.objects.create(company=self.b, provider_config=self.config_b, model_id="qwen3:4b", display_name="Other model", is_default=True)
        self.client = APIClient()

    def test_company_members_only_see_their_models(self):
        self.client.force_authenticate(self.member)
        response = self.client.get("/api/ai/company-models/")
        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data.get("results", response.data) if isinstance(response.data, dict) else response.data
        self.assertEqual({row["model_id"] for row in rows}, {"qwen3:8b"})

    def test_user_cannot_grant_models(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post("/api/ai/company-models/", {
            "company": str(self.b.uuid), "provider_config": str(self.config_b.uuid),
            "model_id": "qwen3:8b", "display_name": "Not approved",
        })
        self.assertEqual(response.status_code, 403)

    def test_user_cannot_select_other_tenants_model(self):
        with self.assertRaises(ValidationError):
            CompanyModelAccessService.resolve(self.member, self.grant_b.uuid)

    def test_admin_sets_tenant_default_and_other_user_cannot(self):
        second = CompanyModelGrant.objects.create(
            company=self.a, provider_config=self.config_a, model_id="qwen3:4b", display_name="Second"
        )
        path = f"/api/ai/company-models/{second.uuid}/set-default/"
        self.client.force_authenticate(self.member)
        self.assertEqual(self.client.post(path).status_code, 403)
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.post(path).status_code, 200)
        self.grant_a.refresh_from_db()
        second.refresh_from_db()
        self.assertFalse(self.grant_a.is_default)
        self.assertTrue(second.is_default)

    def test_granted_model_overrides_provider_default(self):
        second = CompanyModelGrant.objects.create(
            company=self.a, provider_config=self.config_a, model_id="qwen3:4b", display_name="Second"
        )
        config, selected = CompanyModelAccessService.resolve(self.member, second.uuid)
        self.assertEqual(config.model_name, "qwen3:4b")
        self.assertEqual(self.config_a.model_name, "qwen3:8b")
        self.assertTrue(config._entitlement_enforced)
        self.assertEqual(selected.pk, second.pk)

    def test_revoked_model_is_rejected(self):
        self.grant_a.is_enabled = False
        self.grant_a.is_default = False
        self.grant_a.save()
        with self.assertRaises(ValidationError):
            CompanyModelAccessService.resolve(self.member, self.grant_a.uuid)

    def test_cross_tenant_provider_link_is_invalid(self):
        with self.assertRaises(Exception):
            CompanyModelGrant.objects.create(company=self.a, provider_config=self.config_b, model_id="qwen3:4b", display_name="Invalid")


class ProviderTransportTests(TestCase):
    """No live paid API calls: validate fixed endpoint and selected ID."""
    def test_groq_oss_model_uses_groq_chat_endpoint(self):
        from unittest.mock import patch, Mock
        from apps.ai_engine.services.llm_cloud_providers import GroqLLMProvider
        config = Mock()
        config.get_api_key.return_value = "fake-unit-test-key"
        config.model_name = "openai/gpt-oss-120b"
        config.max_tokens = 1024
        config.temperature = 0.2
        response = Mock()
        response.json.return_value = {
            "choices": [{"message": {"content": "Confirmed"}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
        }
        with patch("apps.ai_engine.services.llm_cloud_providers.requests.post", return_value=response) as post:
            result = GroqLLMProvider.generate_answer("Question", [], prompt="Prompt", config=config)
        self.assertEqual(result["provider"], "groq")
        self.assertEqual(result["model"], "openai/gpt-oss-120b")
        self.assertEqual(post.call_args.args[0], "https://api.groq.com/openai/v1/chat/completions")
        self.assertEqual(post.call_args.kwargs["json"]["model"], "openai/gpt-oss-120b")
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer fake-unit-test-key")


class SafeRoutingTests(TestCase):
    def test_firewall_classifies_new_providers_as_cloud(self):
        from apps.data_firewall.services.context_policy_enforcement_service import AIContextPolicyEnforcementService
        self.assertTrue({"groq", "gemini", "anthropic"}.issubset(AIContextPolicyEnforcementService.CLOUD_PROVIDERS))

    def test_serializer_keeps_scope_fields_and_accepts_model_uuid(self):
        from apps.ai_engine.serializers.ai_chat_serializers import AIChatRequestSerializer
        selected = uuid.uuid4()
        s = AIChatRequestSerializer(data={"message": "Hello", "model_uuid": str(selected), "document_scope": "all"})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data["model_uuid"], selected)

    def test_explicit_model_cannot_be_changed_by_generation_options(self):
        from unittest.mock import patch, Mock
        from apps.ai_engine.services.llm_gateway_service import LLMGatewayService, OllamaLLMProvider
        config = Mock(provider_type="ollama", model_name="allowed:8b")
        config._entitlement_enforced = True
        with patch.object(OllamaLLMProvider, "generate_answer", return_value={}) as mock_gen:
            LLMGatewayService.generate_answer("question", [], provider_config=config, generation_options={"model": "unapproved:9b"})
        self.assertNotIn("model", mock_gen.call_args.kwargs["generation_options"])


class GrantRevocationAndAdminTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.company = Company.objects.create(name="Revocation Tenant", slug="revocation-tenant", allow_cloud_llm=True, allow_local_llm=True)
        self.member = User.objects.create_user(username="revocation-member", password="pw", company=self.company, role="member")
        self.admin = User.objects.create_user(username="revocation-admin", password="pw", company=self.company, role="company_admin")
        self.provider = LLMProviderConfig.objects.create(company=self.company, name="Test Ollama", provider_type="ollama", model_name="qwen3:8b")
        self.grant = CompanyModelGrant.objects.create(company=self.company, provider_config=self.provider, model_id="qwen3:8b", display_name="Allowed", is_default=True)
        self.client = APIClient()

    def test_no_fallback_after_last_grant_revoked(self):
        from rest_framework.exceptions import PermissionDenied
        self.grant.is_enabled = False
        self.grant.is_default = False
        self.grant.save()
        self.grant.soft_delete()
        with self.assertRaises(PermissionDenied):
            CompanyModelAccessService.resolve(self.member)

    def test_company_admin_cannot_write_provider_configuration(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.patch(f"/api/ai/llm-providers/{self.provider.uuid}/", {"model_name": "unapproved-model"}, format="json")
        self.assertEqual(resp.status_code, 403)
