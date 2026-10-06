import json
import logging
import os
import time

import requests

from apps.ai_engine.services.external_ai_model_policy import ExternalAIModelPolicy

logger = logging.getLogger(__name__)


class ExternalSemanticOrchestratorService:
    ACTION_DIRECT = "direct"
    ACTION_DOCUMENT = "document"
    ACTION_WEB = "web"
    ACTION_DOCUMENT_WEB = "document_web"
    ACTION_CLARIFY = "clarify"

    DOCUMENT_DEPENDENCY_NONE = "none"
    DOCUMENT_DEPENDENCY_REQUIRED = "required"
    DOCUMENT_DEPENDENCY_OPTIONAL = "optional"

    VALID_DOCUMENT_DEPENDENCIES = {
        DOCUMENT_DEPENDENCY_NONE,
        DOCUMENT_DEPENDENCY_REQUIRED,
        DOCUMENT_DEPENDENCY_OPTIONAL,
    }

    VALID_ACTIONS = {
        ACTION_DIRECT,
        ACTION_DOCUMENT,
        ACTION_WEB,
        ACTION_DOCUMENT_WEB,
        ACTION_CLARIFY,
    }

    VALID_WEB_FRESHNESS = {
        "none",
        "current",
        "recent",
        "historical",
    }

    VALID_WEB_QUERY_TYPES = {
        "stable_knowledge",
        "current_fact",
        "news",
        "article_lookup",
        "source_lookup",
        "web_research",
    }

    QUALITY_FOCUS_API = "api_correctness"
    QUALITY_FOCUS_CONCURRENCY = "concurrency"
    QUALITY_FOCUS_FAILURES = "failure_modes"
    QUALITY_FOCUS_STATE = "state_semantics"
    QUALITY_FOCUS_LIFECYCLE = "resource_lifecycle"
    QUALITY_FOCUS_SECURITY = "security"
    QUALITY_FOCUS_TESTING = "testing"
    QUALITY_FOCUS_PERFORMANCE = "performance"

    VALID_QUALITY_FOCUS = {
        QUALITY_FOCUS_API,
        QUALITY_FOCUS_CONCURRENCY,
        QUALITY_FOCUS_FAILURES,
        QUALITY_FOCUS_STATE,
        QUALITY_FOCUS_LIFECYCLE,
        QUALITY_FOCUS_SECURITY,
        QUALITY_FOCUS_TESTING,
        QUALITY_FOCUS_PERFORMANCE,
    }

    RESPONSE_SCHEMA = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    ACTION_DIRECT,
                    ACTION_DOCUMENT,
                    ACTION_WEB,
                    ACTION_DOCUMENT_WEB,
                    ACTION_CLARIFY,
                ],
            },
            "clarification": {"type": "string"},
            "fast_answer": {"type": "string"},
            "document_query": {"type": "string"},
            "web_query": {"type": "string"},
            "web_required": {
                "type": "boolean",
            },
            "web_freshness": {
                "type": "string",
                "enum": sorted(VALID_WEB_FRESHNESS),
            },
            "web_query_type": {
                "type": "string",
                "enum": sorted(VALID_WEB_QUERY_TYPES),
            },
            "web_resolved_question": {
                "type": "string",
            },
            "web_requested_source": {
                "type": "string",
            },
            "web_exact_title": {
                "type": "string",
            },
            "web_claim_to_verify": {
                "type": "string",
            },
            "web_search_queries": {
                "type": "array",
                "items": {
                    "type": "string",
                },
                "maxItems": 4,
            },
            "document_task": {
                "type": "string",
                "enum": ["none", "qa", "extract", "summarize", "analyze"],
            },
            "document_dependency": {
                "type": "string",
                "enum": [
                    DOCUMENT_DEPENDENCY_NONE,
                    DOCUMENT_DEPENDENCY_REQUIRED,
                    DOCUMENT_DEPENDENCY_OPTIONAL,
                ],
            },
            "answer_tier": {
                "type": "string",
                "enum": ["fast", "quality"],
            },
            # task_domain and reasoning_mode are deliberately orthogonal.
            # This prevents a coding request that discusses concurrency/security
            # from being misrouted as a generic reasoning task.
            "task_domain": {
                "type": "string",
                "enum": ["general", "coding"],
            },
            "reasoning_mode": {
                "type": "string",
                "enum": ["none", "standard", "deep"],
            },
            "quality_focus": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": sorted(VALID_QUALITY_FOCUS),
                },
                "maxItems": 4,
            },
        },
        "required": [
            "action",
            "clarification",
            "fast_answer",
            "document_query",
            "web_query",
            "web_required",
            "document_task",
            "document_dependency",
            "answer_tier",
            "task_domain",
            "reasoning_mode",
            "quality_focus",
        ],
        "additionalProperties": False,
    }

    @classmethod
    def plan(
        cls,
        message,
        conversation=None,
        current_user_message_uuid=None,
        has_attached_documents=False,
        has_explicit_documents=False,
        use_web=True,
        model_override=None,
        allow_quality_fallback=True,
        document_descriptors=None,
        document_scope_mode="none",
        ignore_history=False,
    ):
        model = model_override or ExternalAIModelPolicy.orchestrator_model()
        base_url = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434").rstrip("/")
        timeout = int(os.getenv("OLLAMA_ORCHESTRATOR_TIMEOUT_SECONDS", "120"))

        history = (
            []
            if ignore_history
            else cls._build_history(
                conversation=conversation,
                current_user_message_uuid=current_user_message_uuid,
            )
        )

        messages = [
            {
                "role": "system",
                "content": cls._build_system_prompt(
                    has_attached_documents=has_attached_documents,
                    has_explicit_documents=has_explicit_documents,
                    use_web=use_web,
                    document_descriptors=document_descriptors,
                    document_scope_mode=document_scope_mode,
                ),
            },
        ]
        messages.extend(history)
        messages.append({"role": "user", "content": message})

        started_at = time.perf_counter()

        try:
            response = requests.post(
                f"{base_url}/api/chat",
                json={
                    "model": model,
                    "stream": False,
                    "think": False,
                    "keep_alive": os.getenv("OLLAMA_REQUEST_KEEP_ALIVE", "60m"),
                    "format": cls.RESPONSE_SCHEMA,
                    "messages": messages,
                    "options": {
                        "temperature": 0,
                        "num_ctx": int(
                            os.getenv(
                                "OLLAMA_ORCHESTRATOR_NUM_CTX",
                                os.getenv("OLLAMA_NUM_CTX", "4096"),
                            )
                        ),
                        "num_predict": int(
                            os.getenv("OLLAMA_ORCHESTRATOR_MAX_TOKENS", "360")
                        ),
                    },
                },
                timeout=timeout,
            )
            response.raise_for_status()

            data = response.json()
            content = data.get("message", {}).get("content", "").strip()
            result = json.loads(content)

            normalized = cls._normalize(
                result=result,
                has_attached_documents=has_attached_documents,
                has_explicit_documents=has_explicit_documents,
                use_web=use_web,
            )

            prompt_tokens = int(data.get("prompt_eval_count", 0) or 0)
            completion_tokens = int(data.get("eval_count", 0) or 0)
            wall_duration_ms = round((time.perf_counter() - started_at) * 1000, 2)

            fast_answer_used = bool(
                normalized["action"] == cls.ACTION_DIRECT
                and normalized.get("answer_tier") == "fast"
                and normalized.get("fast_answer")
            )

            normalized.update(
                {
                    "provider": "ollama",
                    "model": model,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                    "metadata": {
                        "semantic_orchestrator": True,
                        "classifier_only": not fast_answer_used,
                        "bounded_fast_answer": fast_answer_used,
                        "wall_duration_ms": wall_duration_ms,
                        "load_duration": data.get("load_duration", 0),
                        "prompt_eval_duration": data.get("prompt_eval_duration", 0),
                        "eval_duration": data.get("eval_duration", 0),
                        "history_message_count": len(history),
                    },
                }
            )

            logger.info(
                "external_ai_orchestrator_complete model=%s action=%s answer_tier=%s "
                "document_dependency=%s task_domain=%s answer_profile=%s "
                "reasoning_mode=%s quality_focus=%s fast_answer=%s wall_ms=%s "
                "prompt_tokens=%s completion_tokens=%s",
                model,
                normalized["action"],
                normalized.get("answer_tier"),
                normalized.get("document_dependency"),
                normalized.get("task_domain"),
                normalized.get("answer_profile"),
                normalized.get("reasoning_mode"),
                ",".join(normalized.get("quality_focus", [])) or "none",
                fast_answer_used,
                wall_duration_ms,
                prompt_tokens,
                completion_tokens,
            )
            return normalized

        except Exception as exc:
            wall_duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
            logger.warning(
                "external_ai_orchestrator_failed model=%s wall_ms=%s error=%s",
                model,
                wall_duration_ms,
                exc,
            )

            quality_model = ExternalAIModelPolicy.quality_text_model()
            if (
                allow_quality_fallback
                and model != quality_model
                and os.getenv("OLLAMA_ORCHESTRATOR_QUALITY_FALLBACK", "True").lower()
                in {"true", "1", "yes"}
            ):
                logger.warning(
                    "external_ai_orchestrator_quality_fallback from_model=%s to_model=%s",
                    model,
                    quality_model,
                )
                fallback = cls.plan(
                    message=message,
                    conversation=conversation,
                    current_user_message_uuid=current_user_message_uuid,
                    has_attached_documents=has_attached_documents,
                    has_explicit_documents=has_explicit_documents,
                    use_web=use_web,
                    model_override=quality_model,
                    allow_quality_fallback=False,
                    document_descriptors=document_descriptors,
                    document_scope_mode=document_scope_mode,
                    ignore_history=ignore_history,
                )
                fallback.setdefault("metadata", {})[
                    "orchestrator_fallback_from_model"
                ] = model
                return fallback

            return {
                "action": cls.ACTION_CLARIFY,
                "clarification": (
                    "I couldn't determine the safest way to handle that request. "
                    "Please try again."
                ),
                "fast_answer": "",
                "document_query": "",
                "web_query": "",
                "document_task": "none",
                "document_dependency": cls.DOCUMENT_DEPENDENCY_NONE,
                "answer_tier": "fast",
                "task_domain": "general",
                "answer_profile": "general",
                "reasoning_mode": "none",
                "quality_focus": [],
                "quality_requirements": [],
                "provider": "system",
                "model": "semantic_orchestrator_fallback",
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "metadata": {
                    "semantic_orchestrator": True,
                    "classifier_only": True,
                    "bounded_fast_answer": False,
                    "wall_duration_ms": wall_duration_ms,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            }

    @classmethod
    def plan_resilient(
        cls,
        message,
        conversation=None,
        current_user_message_uuid=None,
        has_attached_documents=False,
        has_explicit_documents=False,
        use_web=True,
        model_override=None,
        allow_quality_fallback=True,
        document_descriptors=None,
        document_scope_mode="none",
    ):
        """Build a routing plan with one bounded recovery pass.

        The recovery is intentionally semantic rather than phrase-based. If the
        first pass asks for clarification even though authorized document
        descriptors are present, the router is retried once without conversation
        history. This removes stale assistant capability failures while preserving
        normal direct/web routing for unrelated questions.
        """
        first = cls.plan(
            message=message,
            conversation=conversation,
            current_user_message_uuid=current_user_message_uuid,
            has_attached_documents=has_attached_documents,
            has_explicit_documents=has_explicit_documents,
            use_web=use_web,
            model_override=model_override,
            allow_quality_fallback=allow_quality_fallback,
            document_descriptors=document_descriptors,
            document_scope_mode=document_scope_mode,
        )

        documents_available = bool(
            has_attached_documents or has_explicit_documents or document_descriptors
        )

        clarify_recovery = (
            documents_available
            and first.get("action") == cls.ACTION_CLARIFY
            and first.get("document_dependency") != cls.DOCUMENT_DEPENDENCY_REQUIRED
        )

        explicit_document_verification = (
            bool(has_explicit_documents)
            and first.get("action") in {cls.ACTION_DIRECT, cls.ACTION_CLARIFY}
            and first.get("document_dependency") != cls.DOCUMENT_DEPENDENCY_REQUIRED
        )

        if not (clarify_recovery or explicit_document_verification):
            return first

        recovered = cls.plan(
            message=message,
            conversation=conversation,
            current_user_message_uuid=current_user_message_uuid,
            has_attached_documents=has_attached_documents,
            has_explicit_documents=has_explicit_documents,
            use_web=use_web,
            model_override=model_override,
            allow_quality_fallback=False,
            document_descriptors=document_descriptors,
            document_scope_mode=document_scope_mode,
            ignore_history=True,
        )

        recovered_requires_documents = recovered.get(
            "document_dependency"
        ) == cls.DOCUMENT_DEPENDENCY_REQUIRED or recovered.get("action") in {
            cls.ACTION_DOCUMENT,
            cls.ACTION_DOCUMENT_WEB,
        }

        if explicit_document_verification and not clarify_recovery:
            if not recovered_requires_documents:
                return first

        metadata = dict(recovered.get("metadata") or {})
        metadata["routing_recovery"] = {
            "performed": True,
            "reason": (
                "clarify_with_available_documents"
                if clarify_recovery
                else "verify_explicit_document_dependency"
            ),
            "initial_action": first.get("action"),
            "initial_document_dependency": first.get("document_dependency"),
        }
        recovered["metadata"] = metadata
        logger.info(
            "external_ai_orchestrator_recovery initial_action=%s recovered_action=%s "
            "recovered_document_dependency=%s document_scope_mode=%s",
            first.get("action"),
            recovered.get("action"),
            recovered.get("document_dependency"),
            document_scope_mode,
        )
        return recovered

    @classmethod
    def _normalize(
        cls,
        result,
        has_attached_documents,
        has_explicit_documents,
        use_web,
    ):
        action = result.get("action", cls.ACTION_DIRECT)
        clarification = (result.get("clarification") or "").strip()
        fast_answer = (result.get("fast_answer") or "").strip()
        document_query = (result.get("document_query") or "").strip()
        web_query = (result.get("web_query") or "").strip()
        web_required = bool(result.get("web_required"))
        web_freshness = str(result.get("web_freshness") or "none").strip().lower()
        web_query_type = (
            str(result.get("web_query_type") or "stable_knowledge").strip().lower()
        )
        web_resolved_question = str(result.get("web_resolved_question") or "").strip()[
            :900
        ]
        web_requested_source = str(result.get("web_requested_source") or "").strip()[
            :180
        ]
        web_exact_title = str(result.get("web_exact_title") or "").strip()[:500]
        web_claim_to_verify = str(result.get("web_claim_to_verify") or "").strip()[:900]

        raw_web_queries = result.get("web_search_queries") or []

        if isinstance(
            raw_web_queries,
            str,
        ):
            raw_web_queries = [raw_web_queries]

        web_search_queries = []
        seen_web_queries = set()

        if isinstance(
            raw_web_queries,
            (
                list,
                tuple,
            ),
        ):
            for item in raw_web_queries:
                value = " ".join(str(item or "").split())[:700]

                key = value.casefold()

                if not value or key in seen_web_queries:
                    continue

                seen_web_queries.add(key)

                web_search_queries.append(value)

                if len(web_search_queries) >= 4:
                    break

        document_task = (result.get("document_task") or "none").strip().lower()
        document_dependency = (
            (result.get("document_dependency") or cls.DOCUMENT_DEPENDENCY_NONE)
            .strip()
            .lower()
        )
        answer_tier = (result.get("answer_tier") or "quality").strip().lower()
        reasoning_mode = (result.get("reasoning_mode") or "standard").strip().lower()

        # New schema: task domain and reasoning depth are separate dimensions.
        # Keep legacy answer_profile compatibility so existing tests/stored plans do
        # not break during the migration.
        task_domain = (result.get("task_domain") or "").strip().lower()
        legacy_profile = (result.get("answer_profile") or "").strip().lower()
        if task_domain not in {"general", "coding"}:
            task_domain = "coding" if legacy_profile == "coding" else "general"

        raw_focus = result.get("quality_focus") or []
        quality_focus = []
        if isinstance(raw_focus, list):
            # Validate and deduplicate before enforcing the four-item bound.
            # Invalid or duplicate entries must not consume bounded slots.
            for item in raw_focus:
                focus = str(item or "").strip().lower()
                if focus in cls.VALID_QUALITY_FOCUS and focus not in quality_focus:
                    quality_focus.append(focus)
                    if len(quality_focus) >= 4:
                        break

        # Legacy free-text criteria are accepted only for compatibility. The new
        # planner no longer generates them because free-form checklist generation
        # made the routing call slower and less reliable.
        legacy_requirements = []
        raw_legacy_requirements = result.get("quality_requirements") or []
        if isinstance(raw_legacy_requirements, list):
            for item in raw_legacy_requirements[:6]:
                text = str(item or "").strip()
                if text:
                    legacy_requirements.append(text[:240])

        valid_document_tasks = {"none", "qa", "extract", "summarize", "analyze"}
        if action not in cls.VALID_ACTIONS:
            action = cls.ACTION_DIRECT
        if document_task not in valid_document_tasks:
            document_task = "qa"
        if document_dependency not in cls.VALID_DOCUMENT_DEPENDENCIES:
            document_dependency = cls.DOCUMENT_DEPENDENCY_NONE
        if answer_tier not in {"fast", "quality"}:
            answer_tier = "quality"
        if reasoning_mode not in {"none", "standard", "deep"}:
            reasoning_mode = "standard"

        if web_freshness not in cls.VALID_WEB_FRESHNESS:
            web_freshness = "none"

        if web_query_type not in cls.VALID_WEB_QUERY_TYPES:
            web_query_type = "web_research" if web_required else "stable_knowledge"

        #
        # web_required is an independent semantic
        # safety decision. A routing-model mismatch
        # such as direct + web_required=True must
        # never become a memory-only answer.
        #
        if web_required:
            if action == cls.ACTION_DIRECT:
                action = cls.ACTION_WEB

            elif action == cls.ACTION_DOCUMENT:
                action = cls.ACTION_DOCUMENT_WEB

        if action in {
            cls.ACTION_WEB,
            cls.ACTION_DOCUMENT_WEB,
        }:
            web_required = True

            if web_freshness == "none":
                web_freshness = "current"

            if not web_query:
                web_query = (
                    web_search_queries[0] if web_search_queries else ""
                ) or web_resolved_question

        elif action != cls.ACTION_CLARIFY:
            web_required = False
            web_freshness = "none"
            web_query_type = "stable_knowledge"
            web_resolved_question = ""
            web_requested_source = ""
            web_exact_title = ""
            web_claim_to_verify = ""
            web_search_queries = []

        documents_available = bool(has_attached_documents or has_explicit_documents)

        # A selected scope is an explicit user instruction for this turn. Do not
        # allow the semantic router to silently discard that scope and answer as
        # a general chat request. The model still classifies the document task,
        # but selected documents must participate in the answer.
        if has_explicit_documents and documents_available:
            document_dependency = cls.DOCUMENT_DEPENDENCY_REQUIRED

            if action == cls.ACTION_WEB:
                action = cls.ACTION_DOCUMENT_WEB if use_web else cls.ACTION_DOCUMENT
            elif action not in {cls.ACTION_DOCUMENT, cls.ACTION_DOCUMENT_WEB}:
                action = cls.ACTION_DOCUMENT

            if document_task == "none":
                document_task = "qa"

        # Document dependency is independent from action. The routing model does
        # not need file contents itself; it only decides whether the downstream
        # document tool is required. This prevents an attachment-aware request
        # from becoming a false clarification merely because the router cannot
        # inspect the attachment directly.
        if document_dependency == cls.DOCUMENT_DEPENDENCY_REQUIRED:
            if not documents_available:
                return cls._non_direct_plan(
                    action=cls.ACTION_CLARIFY,
                    clarification=(
                        "Please attach or select the document you want me to use."
                    ),
                    document_dependency=cls.DOCUMENT_DEPENDENCY_REQUIRED,
                )

            if action in {cls.ACTION_DIRECT, cls.ACTION_CLARIFY}:
                action = cls.ACTION_DOCUMENT
            elif action == cls.ACTION_WEB:
                action = cls.ACTION_DOCUMENT_WEB if use_web else cls.ACTION_DOCUMENT

            if document_task == "none":
                document_task = "qa"

        if (
            action in {cls.ACTION_DOCUMENT, cls.ACTION_DOCUMENT_WEB}
            and not documents_available
        ):
            return cls._non_direct_plan(
                action=cls.ACTION_CLARIFY,
                clarification="Please attach or select the document you want me to use.",
            )

        if action in {cls.ACTION_WEB, cls.ACTION_DOCUMENT_WEB} and not use_web:
            return cls._non_direct_plan(
                action=cls.ACTION_CLARIFY,
                clarification=(
                    "Web search is disabled for this request, so I can't verify "
                    "current external information."
                ),
            )

        if action == cls.ACTION_CLARIFY:
            return cls._non_direct_plan(
                action=cls.ACTION_CLARIFY,
                clarification=clarification
                or "Could you provide a little more detail about what you want me to help with?",
            )

        if action not in {cls.ACTION_DOCUMENT, cls.ACTION_DOCUMENT_WEB}:
            document_task = "none"
        elif document_task == "none":
            document_task = "qa"

        if action != cls.ACTION_DIRECT:
            plan = cls._non_direct_plan(
                action=action,
                document_dependency=document_dependency,
            )
            plan["document_query"] = document_query
            plan["web_query"] = web_query
            plan["web_required"] = web_required
            plan["web_freshness"] = web_freshness
            plan["web_query_type"] = web_query_type
            plan["web_resolved_question"] = web_resolved_question
            plan["web_requested_source"] = web_requested_source
            plan["web_exact_title"] = web_exact_title
            plan["web_claim_to_verify"] = web_claim_to_verify
            plan["web_search_queries"] = web_search_queries
            plan["document_task"] = document_task
            return plan

        # Coding is a task domain, not a competing reasoning profile. Any direct
        # coding task is owned by the dedicated coder model even when the topic is
        # concurrency, security, distributed systems, or algorithms.
        if task_domain == "coding":
            answer_profile = "coding"
            answer_tier = "quality"
            reasoning_mode = "standard"
            fast_answer = ""
            quality_requirements = cls._build_coding_quality_requirements(
                quality_focus=quality_focus,
                legacy_requirements=legacy_requirements,
            )
        elif answer_tier == "fast":
            answer_profile = "general"
            reasoning_mode = "none"
            fast_answer = fast_answer
            quality_focus = []
            quality_requirements = []
        else:
            # Deep reasoning is now only a property of non-coding quality work.
            answer_profile = "reasoning" if reasoning_mode == "deep" else "general"
            if reasoning_mode == "none":
                reasoning_mode = "standard"
            fast_answer = ""
            quality_focus = []
            quality_requirements = []

        return {
            "action": action,
            "clarification": "",
            "fast_answer": fast_answer,
            "document_query": document_query,
            "web_query": web_query,
            "web_required": web_required,
            "web_freshness": web_freshness,
            "web_query_type": web_query_type,
            "web_resolved_question": web_resolved_question,
            "web_requested_source": web_requested_source,
            "web_exact_title": web_exact_title,
            "web_claim_to_verify": web_claim_to_verify,
            "web_search_queries": web_search_queries,
            "document_task": document_task,
            "document_dependency": document_dependency,
            "answer_tier": answer_tier,
            "task_domain": task_domain,
            "answer_profile": answer_profile,
            "reasoning_mode": reasoning_mode,
            "quality_focus": quality_focus,
            "quality_requirements": quality_requirements,
        }

    @classmethod
    def _build_coding_quality_requirements(
        cls,
        quality_focus,
        legacy_requirements=None,
    ):
        """Build bounded deterministic acceptance criteria for coding requests.

        The semantic router chooses only compact focus categories. The backend
        expands them into stable engineering checks, avoiding slow free-form
        checklist generation while still giving the coding model task-relevant
        validation guidance.
        """
        requirements = [
            "The implementation and explanation must agree; remove unused or dead mechanisms and do not describe guarantees the code does not provide.",
            "Verify third-party API names, signatures, argument ordering, return values, and exception types used by the implementation.",
            "Read-only inspection or status helpers must not mutate the state they report unless that behavior is explicitly required and documented.",
        ]

        focus_requirements = {
            cls.QUALITY_FOCUS_API: (
                "Validate external library/client API semantics and imports against the way they are used in the code."
            ),
            cls.QUALITY_FOCUS_CONCURRENCY: (
                "Any shared-state check-and-update sequence that must be race-free must use a genuinely atomic primitive, transaction, or server-side operation."
            ),
            cls.QUALITY_FOCUS_FAILURES: (
                "Define failure behavior explicitly, including timeout/retry boundaries and fail-open versus fail-closed behavior where relevant."
            ),
            cls.QUALITY_FOCUS_STATE: (
                "Preserve the requested state semantics exactly; reads must not consume quota/state and rejected operations must not accidentally mutate admitted state."
            ),
            cls.QUALITY_FOCUS_LIFECYCLE: (
                "Include bounded cleanup/expiry/resource-lifecycle behavior so temporary state does not grow without limit."
            ),
            cls.QUALITY_FOCUS_SECURITY: (
                "Check security boundaries, untrusted inputs, secrets, authorization assumptions, and unsafe defaults that materially apply."
            ),
            cls.QUALITY_FOCUS_TESTING: (
                "Cover important edge cases and, where applicable, concurrency/failure scenarios with executable tests or a concrete test strategy."
            ),
            cls.QUALITY_FOCUS_PERFORMANCE: (
                "Avoid unnecessary network round-trips, unbounded scans, hot-key amplification, or other avoidable scalability bottlenecks."
            ),
        }

        for focus in quality_focus:
            requirement = focus_requirements.get(focus)
            if requirement and requirement not in requirements:
                requirements.append(requirement)

        # Retain old planner-generated criteria during rollout, but cap the final
        # checklist so prompt growth stays bounded.
        for item in legacy_requirements or []:
            if item not in requirements:
                requirements.append(item)

        return requirements[:8]

    @staticmethod
    def _non_direct_plan(
        action,
        clarification="",
        document_dependency="none",
    ):
        return {
            "action": action,
            "clarification": clarification,
            "fast_answer": "",
            "document_query": "",
            "web_query": "",
            "document_task": "none",
            "document_dependency": document_dependency,
            "answer_tier": "fast",
            "task_domain": "general",
            "answer_profile": "general",
            "reasoning_mode": "none",
            "quality_focus": [],
            "quality_requirements": [],
        }

    @classmethod
    def _build_system_prompt(
        cls,
        has_attached_documents,
        has_explicit_documents,
        use_web,
        document_descriptors=None,
        document_scope_mode="none",
    ):
        descriptor_block = cls._format_document_descriptors(document_descriptors)
        return f"""
You are the semantic router for DocuX External AI. Return only the structured
routing object required by the schema.

Action:
- direct: stable conversation, writing, explanation, coding, analysis, or reasoning.
- document: attached/selected document content is required.
- web: current/live externally verified public information is required.
- document_web: both document content and current web evidence are required.
- clarify: essential information is missing.

Choose document_dependency independently from action:
- required: answering correctly requires content from an attached/selected document.
- optional: document content may help, but the request can still be answered correctly without it.
- none: attached/selected document content is not needed.

The router does NOT need to inspect document contents itself. When documents=true,
the downstream document tool can access them after routing. Therefore, when the
user's request depends on an attached/selected file, set document_dependency=required
and route to document (or document_web when current web evidence is also required).
Never use clarify only because the routing model cannot see or access the attachment.
Use the document descriptors (filename, MIME type, state) to resolve semantic references
to an available artifact. The user does not need to repeat the exact filename.
Document scope semantics:
- selected: the frontend supplied files explicitly for THIS turn (selected in the
  attachment panel or uploaded with the current prompt). This is an explicit instruction
  to use those files. Set document_dependency=required and route to document/document_web.
  Do not downgrade a selected-scope request to direct merely because the wording is vague,
  deictic, or short. The downstream document tool can read the selected files.
- conversation: no subset was selected, so the frontend supplied all documents attached
  to the conversation as candidates. Their presence alone does not make them relevant.
- none: no document UUID scope was supplied.
The user does not need to repeat an exact filename when a semantic reference can be
resolved from the available descriptors. Do not force document use for an unrelated
general question merely because files are available.

For direct requests choose answer_tier:
- fast: short, low-complexity requests that can be answered completely in a compact response.
- quality: coding, detailed/long-form work, nuanced analysis, correctness-sensitive work,
  multi-step reasoning, or anything uncertain.

For direct requests choose task_domain independently from reasoning depth:
- coding: source code or software implementation is central to the task, including code
  generation, modification, debugging, review, API/library usage, or implementation design.
- general: source code/software implementation is not central.

Important: coding and reasoning are NOT competing categories. If the user asks you to
write/debug/review/implement code, task_domain=coding even when the code concerns
concurrency, distributed systems, security, algorithms, mathematics, or failure modes.

Choose reasoning_mode:
- none: only for fast direct answers.
- standard: normal quality work and ALL coding tasks; the dedicated coding model handles code.
- deep: only for non-coding correctness-sensitive reasoning where extended validation is useful.

For coding tasks choose up to four quality_focus values that materially apply:
api_correctness, concurrency, failure_modes, state_semantics, resource_lifecycle,
security, testing, performance. These are focus labels only; do not write the solution.
For non-coding tasks, quality_focus must be [].

Rules:
- direct + fast: task_domain=general, reasoning_mode=none, and fast_answer contains the
  complete concise user-facing answer (normally under 100 words).
- direct + coding: answer_tier=quality, task_domain=coding, reasoning_mode=standard,
  fast_answer="".
- direct + general quality: fast_answer=""; use standard or deep as appropriate.
- non-direct: fast_answer="", task_domain=general, reasoning_mode=none, quality_focus=[].
- document/document_web: document_dependency=required.
- clarify for a missing required document is valid only when documents=false.
- clarification is populated only for clarify.
- Web being enabled does not mean web is required. Attached files do not mean they are relevant.
- Never invent document or web content.
- document_task: qa, extract, summarize, analyze; otherwise none.
- Keep document_query and web_query concise.
- web_required is independent from action. Set it true whenever an accurate answer requires current, changing, externally verified public information, factual claim verification against public evidence, or an explicitly requested public web/site/page/article lookup.
- For stable knowledge, writing, coding, explanation, or reasoning that does not depend on current public facts, set web_required=false.
- When web_required=true, action must be web or document_web.
- Use conversation history to resolve follow-ups, pronouns, topic refinements, and previously requested public sources. Previous assistant answers are never factual evidence.
- Preserve user-supplied entities, organizations, jurisdictions, locations, dates, quoted text, titles, and relationships. Do not invent a missing jurisdiction, country, office, organization, date, or identity.
- web_requested_source contains only a source/publisher/organization/site explicitly requested or clearly inherited from conversation. Do not invent a domain.
- web_exact_title contains a specific page/article/report/post/paper title when supplied or clearly referenced.
- web_claim_to_verify contains the factual claim to check when applicable.
- web_resolved_question is a standalone version of the current question after resolving conversation references.
- web_search_queries contains up to 4 concise standalone discovery queries, ordered most precise to broadest. Preserve important literal wording from the user's request.
- web_freshness is none/current/recent/historical.
- web_query_type is stable_knowledge/current_fact/news/article_lookup/source_lookup/web_research.
- For non-web requests use web_required=false, web_freshness=none, web_query_type=stable_knowledge, and empty web-specific strings/arrays.
- In fast_answer speak as DocuX External AI and do not expose the underlying model/vendor.

documents={has_attached_documents or has_explicit_documents}
documents_selected={has_explicit_documents}
document_scope_mode={document_scope_mode}
document_descriptors:
{descriptor_block}
web={use_web}
""".strip()

    @staticmethod
    def _format_document_descriptors(document_descriptors):
        if not document_descriptors:
            return "(none)"

        lines = []
        for item in list(document_descriptors)[:25]:
            if not isinstance(item, dict):
                continue
            lines.append(
                "- uuid={uuid} | filename={filename} | mime={mime} | state={state}".format(
                    uuid=str(item.get("uuid") or "")[:64],
                    filename=str(
                        item.get("original_filename") or item.get("title") or ""
                    )[:180],
                    mime=str(item.get("mime_type") or "")[:100],
                    state=str(item.get("state") or item.get("status") or "")[:40],
                )
            )

        if len(document_descriptors) > 25:
            lines.append(f"- ... {len(document_descriptors) - 25} additional documents")

        return "\n".join(lines) or "(none)"

    @classmethod
    def _build_history(cls, conversation, current_user_message_uuid=None):
        if conversation is None:
            return []

        queryset = conversation.messages.filter(is_deleted=False)
        if current_user_message_uuid:
            queryset = queryset.exclude(uuid=current_user_message_uuid)
        queryset = queryset.order_by("-created_at")[:2]

        history = []
        for message in reversed(list(queryset)):
            if message.role not in {"user", "assistant"}:
                continue

            # Failed capability clarifications such as "I cannot access the
            # screenshot" must never become evidence for a later routing turn.
            # They can create a self-reinforcing loop where a now-ready attachment
            # is repeatedly classified as unavailable.
            if (
                message.role == "assistant"
                and (message.metadata or {}).get("semantic_action")
                == cls.ACTION_CLARIFY
            ):
                continue

            history.append({"role": message.role, "content": message.content[:500]})
        return history
