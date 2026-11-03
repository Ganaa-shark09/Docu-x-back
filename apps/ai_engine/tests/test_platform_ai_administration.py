"""Platform AI admin RBAC, company isolation, and encrypted-key API behavior."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.ai_engine.models import CompanyModelGrant, LLMProviderConfig
from apps.tenants.models import Company


class PlatformAiAdministrationTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.a = Company.objects.create(name="AI Platform Test Alpha", slug="ai-platform-alpha", allow_cloud_llm=True)
        self.b = Company.objects.create(name="AI Platform Test Beta", slug="ai-platform-beta", allow_cloud_llm=True)
        self.superuser = User.objects.create_superuser(username="platform-model-root", email="platform-root@test.invalid", password="pw")
        self.admin = User.objects.create_user(username="platform-test-admin", password="pw", company=self.a, role="company_admin")
        self.member = User.objects.create_user(username="platform-test-member", password="pw", company=self.b, role="member")
        self.client = APIClient()
        self.company_url = "/api/ai/platform/companies/"
        self.providers_url = "/api/ai/platform/providers/"

    def test_only_platform_superuser_can_list_companies(self):
        self.client.force_authenticate(self.member)
        self.assertEqual(self.client.get(self.company_url).status_code, 403)
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.get(self.company_url).status_code, 403)
        self.client.force_authenticate(self.superuser)
        response = self.client.get(self.company_url)
        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data.get("results", [])
        self.assertIn(str(self.a.uuid), {row["uuid"] for row in rows})

    def test_company_admin_cannot_modify_provider_or_policy(self):
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.post(self.providers_url, {"company": str(self.a.uuid), "name": "Unauthorized", "provider_type": "groq"}).status_code, 403)
        self.assertEqual(self.client.patch(f"{self.company_url}{self.a.uuid}/", {"allow_cloud_llm": False}, format="json").status_code, 403)

    def test_superuser_can_set_company_policy(self):
        self.client.force_authenticate(self.superuser)
        response = self.client.patch(f"{self.company_url}{self.a.uuid}/", {"allow_cloud_llm": False, "allow_local_llm": True}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.a.refresh_from_db()
        self.assertFalse(self.a.allow_cloud_llm)
        self.assertTrue(self.a.allow_local_llm)

    def test_create_rotate_provider_credential_without_readback(self):
        self.client.force_authenticate(self.superuser)
        response = self.client.post(self.providers_url, {
            "company": str(self.a.uuid), "name": "Groq Alpha", "provider_type": "groq",
            "model_name": "openai/gpt-oss-120b", "api_key": "test-secret-not-real", "is_active": True,
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data["has_api_key"])
        self.assertNotIn("api_key", response.data)
        self.assertNotIn("api_key_encrypted", response.data)
        provider = LLMProviderConfig.objects.get(uuid=response.data["uuid"])
        self.assertNotEqual(provider.api_key_encrypted, "test-secret-not-real")
        self.assertEqual(provider.get_api_key(), "test-secret-not-real")
        response = self.client.patch(f"{self.providers_url}{provider.uuid}/", {"api_key": "rotated-test-secret"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        provider.refresh_from_db()
        self.assertEqual(provider.get_api_key(), "rotated-test-secret")
        self.assertNotIn("api_key", response.data)
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.get(f"{self.providers_url}?company={self.a.uuid}").status_code, 403)

    def test_provider_list_filters_by_company(self):
        self.client.force_authenticate(self.superuser)
        LLMProviderConfig.objects.create(company=self.a, name="Ollama A", provider_type="ollama", model_name="test:8b")
        LLMProviderConfig.objects.create(company=self.b, name="Ollama B", provider_type="ollama", model_name="test:4b")
        response = self.client.get(f"{self.providers_url}?company={self.a.uuid}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([p["name"] for p in response.data], ["Ollama A"])

    def test_official_cloud_providers_cannot_set_custom_endpoint(self):
        self.client.force_authenticate(self.superuser)
        response = self.client.post(self.providers_url, {
            "company": str(self.a.uuid), "name": "Groq redirect", "provider_type": "groq",
            "endpoint_url": "http://127.0.0.1/internal", "api_key": "fake", "is_active": True,
        }, format="json")
        self.assertEqual(response.status_code, 400, response.data)

    def test_provider_deactivation_removes_grants_from_company_catalog(self):
        provider = LLMProviderConfig.objects.create(company=self.a, name="Ollama A", provider_type="ollama", model_name="qwen3:8b", is_active=True)
        CompanyModelGrant.objects.create(company=self.a, provider_config=provider, model_id="qwen3:8b", display_name="Qwen", is_enabled=True)
        self.client.force_authenticate(self.superuser)
        response = self.client.patch(f"{self.providers_url}{provider.uuid}/", {"is_active": False}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.client.force_authenticate(self.admin)
        response = self.client.get("/api/ai/company-models/")
        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data.get("results", response.data) if isinstance(response.data, dict) else response.data
        self.assertEqual(rows, [])
