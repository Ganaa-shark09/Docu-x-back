import ast
import operator
import re


class ExternalQueryRouterService:
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
        "this file",
        "this document",
        "this screenshot",
        "this image",
    }

    CURRENT_FACT_KEYWORDS = {
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
    }

    OPERATORS = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
        ast.USub: operator.neg,
        ast.UAdd: operator.pos,
    }

    @classmethod
    def classify(
        cls,
        message,
        has_attached_documents=False,
        explicit_document_uuids=None,
        use_web=True,
    ):
        if cls.try_calculate(message) is not None:
            return "calculator"

        if explicit_document_uuids:
            return "document"

        if has_attached_documents and cls.looks_like_document_question(message):
            return "document"

        if use_web and cls.looks_like_current_fact_question(message):
            return "web"

        if use_web:
            return "general_web"

        return "general"

    @classmethod
    def looks_like_document_question(cls, message):
        normalized = message.lower()

        return any(keyword in normalized for keyword in cls.DOCUMENT_KEYWORDS)

    @classmethod
    def looks_like_current_fact_question(cls, message):
        normalized = message.lower()

        return any(keyword in normalized for keyword in cls.CURRENT_FACT_KEYWORDS)

    @classmethod
    def try_calculate(cls, message):
        expression = cls.extract_math_expression(message)

        if not expression:
            return None

        try:
            tree = ast.parse(expression, mode="eval")
            result = cls.safe_eval(tree.body)

            if isinstance(result, float) and result.is_integer():
                result = int(result)

            return result

        except Exception:
            return None

    @classmethod
    def extract_math_expression(cls, message):
        normalized = message.lower()

        normalized = normalized.replace("×", "*")
        normalized = normalized.replace("x", "*")
        normalized = normalized.replace("÷", "/")
        normalized = normalized.replace("plus", "+")
        normalized = normalized.replace("minus", "-")
        normalized = normalized.replace("multiplied by", "*")
        normalized = normalized.replace("multiply by", "*")
        normalized = normalized.replace("divided by", "/")
        normalized = normalized.replace("divide by", "/")

        match = re.search(r"[-+*/().\d\s]{3,}", normalized)

        if not match:
            return None

        expression = match.group(0).strip()

        if not re.search(r"\d", expression):
            return None

        if not re.search(r"[+\-*/]", expression):
            return None

        return expression

    @classmethod
    def safe_eval(cls, node):
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)):
                return node.value
            raise ValueError("Unsupported constant")

        if isinstance(node, ast.BinOp):
            operator_func = cls.OPERATORS.get(type(node.op))
            if not operator_func:
                raise ValueError("Unsupported operator")

            return operator_func(
                cls.safe_eval(node.left),
                cls.safe_eval(node.right),
            )

        if isinstance(node, ast.UnaryOp):
            operator_func = cls.OPERATORS.get(type(node.op))
            if not operator_func:
                raise ValueError("Unsupported unary operator")

            return operator_func(cls.safe_eval(node.operand))

        raise ValueError("Unsupported expression")
