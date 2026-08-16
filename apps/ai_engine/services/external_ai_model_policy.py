import os


class ExternalAIModelPolicy:
    """Central model policy for External AI workloads.

    Small models handle bounded routing/coverage work. Specialized models own
    tasks where domain fit matters, while the general quality model remains the
    safe fallback for user-visible reasoning.
    """

    PROFILE_GENERAL = "general"
    PROFILE_CODING = "coding"
    PROFILE_REASONING = "reasoning"
    VALID_DIRECT_PROFILES = {
        PROFILE_GENERAL,
        PROFILE_CODING,
        PROFILE_REASONING,
    }

    @staticmethod
    def fast_text_model():
        return os.getenv("OLLAMA_FAST_TEXT_MODEL", "qwen3:4b-instruct")

    @staticmethod
    def quality_text_model():
        return os.getenv(
            "OLLAMA_QUALITY_TEXT_MODEL",
            os.getenv("OLLAMA_DEFAULT_MODEL", "qwen3:8b"),
        )

    @staticmethod
    def coding_model():
        return os.getenv("OLLAMA_CODING_MODEL", "qwen2.5-coder:7b")

    @classmethod
    def orchestrator_model(cls):
        return os.getenv("OLLAMA_ORCHESTRATOR_MODEL", cls.fast_text_model())

    @classmethod
    def direct_fast_model(cls):
        return os.getenv("OLLAMA_DIRECT_FAST_MODEL", cls.fast_text_model())

    @classmethod
    def direct_quality_model(cls):
        return os.getenv("OLLAMA_DIRECT_QUALITY_MODEL", cls.quality_text_model())

    @classmethod
    def direct_model(cls, *, answer_tier="quality", answer_profile="general"):
        """Choose the final model for a direct External AI answer.

        Coding uses a dedicated code model. Other quality work stays on the
        stronger general model. Unknown profiles fail closed to general quality.
        """
        profile = (answer_profile or cls.PROFILE_GENERAL).strip().lower()
        if profile not in cls.VALID_DIRECT_PROFILES:
            profile = cls.PROFILE_GENERAL

        if profile == cls.PROFILE_CODING:
            return cls.coding_model()
        if answer_tier == "fast":
            return cls.direct_fast_model()
        return cls.direct_quality_model()

    @classmethod
    def document_reasoning_model(cls):
        return os.getenv("OLLAMA_DOCUMENT_REASONING_MODEL", cls.quality_text_model())

    @classmethod
    def web_answer_model(cls):
        return os.getenv("OLLAMA_WEB_ANSWER_MODEL", cls.quality_text_model())

    @classmethod
    def summary_map_model(cls):
        return os.getenv("OLLAMA_DOCUMENT_SUMMARY_MAP_MODEL", cls.fast_text_model())

    @classmethod
    def summary_final_model(cls):
        return os.getenv(
            "OLLAMA_DOCUMENT_SUMMARY_FINAL_MODEL", cls.quality_text_model()
        )

    @staticmethod
    def fast_vision_model():
        return os.getenv("OLLAMA_VISION_FAST_MODEL", "qwen3-vl:4b-instruct")

    @staticmethod
    def quality_vision_model():
        return os.getenv(
            "OLLAMA_VISION_QUALITY_MODEL",
            os.getenv("OLLAMA_VISION_MODEL", "qwen3-vl:8b"),
        )
