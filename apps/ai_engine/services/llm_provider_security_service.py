import ipaddress
import os
import re
from urllib.parse import urlsplit


class LLMProviderSecurityService:
    """
    Validates outbound LLM endpoints before credentials or prompts
    can be sent to them.
    """

    AZURE_PROVIDER = "azure_openai"
    OPENAI_PROVIDER = "openai"
    OLLAMA_PROVIDER = "ollama"
    LOCAL_PROVIDER = "local"

    DEPLOYMENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,150}$")
    API_VERSION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:-preview)?$")

    @classmethod
    def azure_allowed_host_suffixes(cls):
        raw = os.getenv(
            "AZURE_OPENAI_ALLOWED_HOST_SUFFIXES",
            ".openai.azure.com",
        )

        return tuple(
            suffix.strip().lower() for suffix in raw.split(",") if suffix.strip()
        )

    @classmethod
    def validate_endpoint(cls, provider_type, endpoint_url):
        provider_type = str(provider_type or "").strip().lower()
        endpoint_url = str(endpoint_url or "").strip()

        if provider_type == cls.LOCAL_PROVIDER:
            return ""

        if provider_type == cls.OPENAI_PROVIDER:
            if endpoint_url:
                raise ValueError(
                    "OpenAI uses the server-controlled "
                    "https://api.openai.com endpoint. "
                    "Custom endpoint_url is not allowed."
                )

            return ""

        if provider_type == cls.AZURE_PROVIDER:
            return cls._validate_azure_endpoint(endpoint_url)

        if provider_type == cls.OLLAMA_PROVIDER:
            return cls._validate_ollama_endpoint(endpoint_url)

        raise ValueError("Unsupported LLM provider type.")

    @classmethod
    def _parse_base_url(cls, endpoint_url):
        try:
            parsed = urlsplit(endpoint_url)
        except Exception as exc:
            raise ValueError("Invalid endpoint URL.") from exc

        if not parsed.scheme or not parsed.hostname:
            raise ValueError("Endpoint must include a URL scheme and hostname.")

        if parsed.username or parsed.password:
            raise ValueError("Credentials must not be embedded in endpoint URLs.")

        if parsed.query or parsed.fragment:
            raise ValueError(
                "Endpoint URL must not contain query parameters or fragments."
            )

        return parsed

    @classmethod
    def _validate_azure_endpoint(cls, endpoint_url):
        if not endpoint_url:
            raise ValueError("Azure OpenAI requires endpoint_url.")

        parsed = cls._parse_base_url(endpoint_url)

        if parsed.scheme.lower() != "https":
            raise ValueError("Azure OpenAI endpoint must use HTTPS.")

        if parsed.path not in {"", "/"}:
            raise ValueError(
                "Azure OpenAI endpoint must be the resource base URL "
                "without an additional path."
            )

        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("Invalid Azure OpenAI endpoint port.") from exc

        if port not in {None, 443}:
            raise ValueError("Azure OpenAI endpoint may only use HTTPS port 443.")

        hostname = parsed.hostname.lower().rstrip(".")

        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            raise ValueError(
                "Azure OpenAI endpoint must use an approved hostname, "
                "not a raw IP address."
            )

        allowed_suffixes = cls.azure_allowed_host_suffixes()

        if not any(hostname.endswith(suffix) for suffix in allowed_suffixes):
            raise ValueError("Azure OpenAI endpoint hostname is not approved.")

        return f"https://{hostname}"

    @classmethod
    def _validate_ollama_endpoint(cls, endpoint_url):
        if not endpoint_url:
            return ""

        parsed = cls._parse_base_url(endpoint_url)

        if parsed.scheme.lower() not in {"http", "https"}:
            raise ValueError("Ollama endpoint must use HTTP or HTTPS.")

        hostname = parsed.hostname

        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("Invalid Ollama endpoint port.") from exc

        authority = hostname

        if ":" in hostname and not hostname.startswith("["):
            authority = f"[{hostname}]"

        if port:
            authority = f"{authority}:{port}"

        path = parsed.path.rstrip("/")

        return f"{parsed.scheme.lower()}://{authority}{path}"

    @classmethod
    def validate_azure_deployment_name(cls, deployment_name):
        deployment_name = str(deployment_name or "").strip()

        if not deployment_name:
            raise ValueError("Azure OpenAI requires deployment_name.")

        if not cls.DEPLOYMENT_RE.fullmatch(deployment_name):
            raise ValueError(
                "Azure deployment_name may contain only letters, "
                "numbers, dots, underscores, and hyphens."
            )

        return deployment_name

    @classmethod
    def validate_azure_api_version(cls, api_version):
        api_version = str(api_version or "").strip()

        if not api_version:
            raise ValueError("Azure OpenAI requires api_version.")

        if not cls.API_VERSION_RE.fullmatch(api_version):
            raise ValueError(
                "Azure api_version must look like YYYY-MM-DD " "or YYYY-MM-DD-preview."
            )

        return api_version
