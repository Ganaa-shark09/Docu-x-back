from apps.ai_engine.services.calculator_tool_service import CalculatorToolService


class ExternalIntentRouterService:
    DOCUMENT_KEYWORDS = {
        "document",
        "file",
        "uploaded",
        "attached",
        "attachment",
        "pdf",
        "excel",
        "word",
        "docx",
        "xlsx",
        "image",
        "screenshot",
        "photo",
        "picture",
        "content",
        "summarize",
        "summary",
        "invoice",
        "contract",
        "receipt",
        "payment term",
        "this file",
        "this document",
        "this screenshot",
        "this image",
        "read the screenshot",
        "read this",
        "summarize the image",
        "summarize image",
        "summary of image",
        "summary of screenshot",
        "what is visible",
        "what is shown",
        "what can you see",
        "describe image",
        "describe screenshot",
        "screen content",
        "page content",
        "modal",
        "dialog",
        "visible text",
        "ocr",
    }

    PUBLIC_WEB_KEYWORDS = {
        "current",
        "latest",
        "today",
        "now",
        "news",
        "prime minister",
        "president",
        "ceo",
        "price",
        "weather",
        "stock",
        "score",
        "version",
        "released",
        "release date",
        "who is",
        "what is",
        "where is",
        "when is",
        "biography",
        "profile",
    }

    @classmethod
    def route(
        cls,
        message,
        has_attached_documents=False,
        explicit_document_uuids=None,
        use_web=True,
    ):
        calculation = CalculatorToolService.calculate(message)
        if calculation:
            return {
                "intent": "calculator",
                "calculation": calculation,
                "should_use_documents": False,
                "should_use_web": False,
                "should_use_llm": False,
            }

        if explicit_document_uuids:
            return {
                "intent": "document_question",
                "calculation": None,
                "should_use_documents": True,
                "should_use_web": False,
                "should_use_llm": True,
            }

        if has_attached_documents and cls.looks_like_document_question(message):
            return {
                "intent": "document_question",
                "calculation": None,
                "should_use_documents": True,
                "should_use_web": False,
                "should_use_llm": True,
            }

        if has_attached_documents:
            return {
                "intent": "hybrid",
                "calculation": None,
                "should_use_documents": True,
                "should_use_web": bool(use_web),
                "should_use_llm": True,
            }

        if use_web and cls.looks_like_web_question(message):
            return {
                "intent": "web_question",
                "calculation": None,
                "should_use_documents": False,
                "should_use_web": True,
                "should_use_llm": True,
            }

        if use_web:
            return {
                "intent": "general_web",
                "calculation": None,
                "should_use_documents": False,
                "should_use_web": True,
                "should_use_llm": True,
            }

        return {
            "intent": "general",
            "calculation": None,
            "should_use_documents": False,
            "should_use_web": False,
            "should_use_llm": True,
        }

    @classmethod
    def looks_like_document_question(cls, message):
        normalized = message.lower()

        return any(keyword in normalized for keyword in cls.DOCUMENT_KEYWORDS)

    @classmethod
    def looks_like_web_question(cls, message):
        normalized = message.lower()

        return any(keyword in normalized for keyword in cls.PUBLIC_WEB_KEYWORDS)
