import ast
import operator
import re


class CalculatorToolService:
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
    def calculate(cls, message):
        expression = cls.extract_expression(message)

        if not expression:
            return None

        try:
            tree = ast.parse(expression, mode="eval")
            result = cls.safe_eval(tree.body)

            if isinstance(result, float) and result.is_integer():
                result = int(result)

            return {
                "expression": expression,
                "result": result,
                "answer": f"The answer is {result}.",
            }

        except Exception:
            return None

    @classmethod
    def extract_expression(cls, message):
        normalized = message.lower().strip()

        normalized = normalized.replace("×", "*")
        normalized = normalized.replace("÷", "/")

        word_expression = cls.extract_word_expression(normalized)
        if word_expression:
            return word_expression

        direct_expression = cls.extract_direct_expression(normalized)
        if direct_expression:
            return direct_expression

        return None

    @classmethod
    def extract_word_expression(cls, normalized):
        number = r"(-?\d+(?:\.\d+)?)"

        patterns = [
            (
                rf"(?:sum|add|addition)\s+(?:of\s+)?{number}\s+(?:and|to|,)\s+{number}",
                "+",
            ),
            (
                rf"(?:subtract|subtraction|minus)\s+(?:of\s+)?{number}\s+(?:and|from|,)\s+{number}",
                "-",
            ),
            (
                rf"(?:multiply|multiplication|product)\s+(?:of\s+)?{number}\s+(?:and|by|,)\s+{number}",
                "*",
            ),
            (rf"{number}\s+(?:times|multiplied by|multiply by)\s+{number}", "*"),
            (
                rf"(?:divide|division)\s+(?:of\s+)?{number}\s+(?:by|and|,)\s+{number}",
                "/",
            ),
            (rf"{number}\s+(?:divided by)\s+{number}", "/"),
        ]

        for pattern, op in patterns:
            match = re.search(pattern, normalized)
            if match:
                left = match.group(1)
                right = match.group(2)

                if "subtract" in pattern and "from" in normalized:
                    return f"{right} - {left}"

                return f"{left} {op} {right}"

        # Handles "sum of 1 2 3" or "add 1 2 3"
        if any(keyword in normalized for keyword in ["sum", "add", "addition"]):
            numbers = re.findall(number, normalized)
            if len(numbers) >= 2:
                return " + ".join(numbers)

        if any(
            keyword in normalized
            for keyword in ["product", "multiply", "multiplication"]
        ):
            numbers = re.findall(number, normalized)
            if len(numbers) >= 2:
                return " * ".join(numbers)

        return None

    @classmethod
    def extract_direct_expression(cls, normalized):
        expression_match = re.search(r"[-+*/().\d\s]{3,}", normalized)

        if not expression_match:
            return None

        expression = expression_match.group(0).strip()

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
