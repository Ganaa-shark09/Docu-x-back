from unittest.mock import (
    patch,
)

from django.test import (
    SimpleTestCase,
)

from apps.ai_engine.services.external_semantic_orchestrator_service import (
    ExternalSemanticOrchestratorService,
)

from apps.ai_engine.services.external_web_routing_guard_service import (
    ExternalWebRoutingGuardService,
)


class ExternalWebRoutingGuardServiceTests(SimpleTestCase):
    def test_todays_news_requires_web(
        self,
    ):
        self.assertTrue(
            ExternalWebRoutingGuardService.requires_web(
                ("What is today's news " "in Example Publication?")
            )
        )

    def test_current_public_fact_requires_web(
        self,
    ):
        self.assertTrue(
            ExternalWebRoutingGuardService.requires_web(
                ("Is Example Person " "the current office holder?")
            )
        )

    def test_stable_explanation_does_not_force_web(
        self,
    ):
        self.assertFalse(
            ExternalWebRoutingGuardService.requires_web("Explain recursion")
        )

    def test_short_followup_inherits_live_context(
        self,
    ):
        self.assertTrue(
            ExternalWebRoutingGuardService.requires_web(
                message="What about politics?",
                previous_user_messages=[
                    ("What is today's news " "in Example Publication?")
                ],
            )
        )

    def test_existing_document_route_becomes_document_web(
        self,
    ):
        result = ExternalWebRoutingGuardService.enforce_plan(
            plan={
                "action": "document",
                "document_dependency": "required",
            },
            message="Compare this with today's news",
            use_web=True,
        )

        self.assertEqual(
            result["action"],
            "document_web",
        )

        self.assertTrue(result["web_required"])

    @patch.object(
        ExternalSemanticOrchestratorService,
        "plan",
        side_effect=AssertionError("model router must not run"),
    )
    def test_obvious_live_query_bypasses_model_router(
        self,
        _plan,
    ):
        result = ExternalSemanticOrchestratorService.plan_resilient(
            message=("What is today's news " "in Example Publication?"),
            conversation=None,
            has_attached_documents=False,
            has_explicit_documents=False,
            use_web=True,
            document_descriptors=[],
            document_scope_mode="none",
        )

        self.assertEqual(
            result["action"],
            "web",
        )

        self.assertTrue(result["web_required"])
