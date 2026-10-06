from django.test import (
    SimpleTestCase,
)

from apps.ai_engine.services.external_semantic_orchestrator_service import (
    ExternalSemanticOrchestratorService,
)

from apps.ai_engine.services.external_web_plan_service import (
    ExternalWebPlanService,
)


class ExternalWebPlanServiceTests(SimpleTestCase):
    def test_web_required_overrides_stale_direct_action(
        self,
    ):
        result = ExternalSemanticOrchestratorService._normalize(
            result={
                "action": "direct",
                "web_required": True,
                "web_freshness": "current",
                "web_query_type": "current_fact",
                "web_resolved_question": ("Verify a changing " "public claim"),
                "web_claim_to_verify": ("A changing " "public claim"),
                "web_search_queries": [("changing " "public claim")],
            },
            has_attached_documents=False,
            has_explicit_documents=False,
            use_web=True,
        )

        self.assertEqual(
            result["action"],
            "web",
        )

        self.assertTrue(result["web_required"])

    def test_selected_document_plus_web_requirement_becomes_document_web(
        self,
    ):
        result = ExternalSemanticOrchestratorService._normalize(
            result={
                "action": "direct",
                "web_required": True,
                "web_freshness": "recent",
                "web_query_type": "web_research",
            },
            has_attached_documents=True,
            has_explicit_documents=True,
            use_web=True,
        )

        self.assertEqual(
            result["action"],
            "document_web",
        )

    def test_adapter_preserves_literal_user_request(
        self,
    ):
        plan = ExternalWebPlanService.from_routing_plan(
            message=("Original Entity is " "current holder right?"),
            routing_plan={
                "action": "web",
                "web_required": True,
                "web_freshness": "current",
                "web_query_type": "current_fact",
                "web_resolved_question": ("Planner " "paraphrase"),
                "web_claim_to_verify": ("Planner claim"),
                "web_search_queries": [("planner " "search query")],
            },
            use_web=True,
        )

        self.assertIn(
            ("Original Entity is " "current holder right?"),
            plan["search_queries"],
        )

    def test_source_and_exact_title_are_generic_data(
        self,
    ):
        plan = ExternalWebPlanService.from_routing_plan(
            message=("Read the requested " "public article"),
            routing_plan={
                "action": "web",
                "web_required": True,
                "web_freshness": "recent",
                "web_query_type": "article_lookup",
                "web_requested_source": "Example Publication",
                "web_exact_title": ("A Specific " "Public Article"),
                "web_search_queries": [],
            },
            use_web=True,
        )

        self.assertEqual(
            plan["requested_source"],
            "Example Publication",
        )

        self.assertEqual(
            plan["exact_title"],
            ("A Specific " "Public Article"),
        )

        self.assertIn(
            ('"A Specific Public Article" ' "Example Publication"),
            plan["search_queries"],
        )

    def test_stable_direct_request_has_no_web_plan(
        self,
    ):
        plan = ExternalWebPlanService.from_routing_plan(
            message="Explain recursion",
            routing_plan={
                "action": "direct",
                "web_required": False,
            },
            use_web=True,
        )

        self.assertFalse(plan["needs_web"])

        self.assertEqual(
            plan["search_queries"],
            [],
        )


class ExternalWebPlanUncertainRouteTests(SimpleTestCase):
    def test_uncertain_route_gets_literal_web_fallback(
        self,
    ):
        plan = ExternalWebPlanService.fallback_for_uncertain_route(
            message=("Is Example Entity " "the current holder?"),
            use_web=True,
        )

        self.assertIsNotNone(plan)

        self.assertTrue(plan["needs_web"])

        self.assertEqual(
            plan["search_queries"],
            [("Is Example Entity " "the current holder?")],
        )

        self.assertEqual(
            plan["fallback_reason"],
            ("semantic_router_" "uncertain"),
        )

    def test_uncertain_route_does_not_bypass_disabled_web(
        self,
    ):
        plan = ExternalWebPlanService.fallback_for_uncertain_route(
            message="Ambiguous request",
            use_web=False,
        )

        self.assertIsNone(plan)

    def test_exact_title_forces_article_lookup_structurally(
        self,
    ):
        plan = ExternalWebPlanService.from_routing_plan(
            message="Read this article",
            routing_plan={
                "action": "web",
                "web_required": True,
                "web_freshness": "recent",
                "web_query_type": "current_fact",
                "web_exact_title": ("A Specific " "Public Article"),
            },
            use_web=True,
        )

        self.assertEqual(
            plan["query_type"],
            "article_lookup",
        )

    def test_current_web_plan_is_not_stable_knowledge(
        self,
    ):
        plan = ExternalWebPlanService.from_routing_plan(
            message="Current public information",
            routing_plan={
                "action": "web",
                "web_required": True,
                "web_freshness": "current",
                "web_query_type": "stable_knowledge",
            },
            use_web=True,
        )

        self.assertEqual(
            plan["query_type"],
            "web_research",
        )
