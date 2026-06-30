import hashlib
import math
import re

from apps.vector_search.models import DocumentChunk


class BaseEmbeddingProvider:
    provider_name = "base"
    dimensions = DocumentChunk.EMBEDDING_DIMENSIONS

    def embed_text(self, text):
        raise NotImplementedError


class LocalHashingEmbeddingProvider(BaseEmbeddingProvider):
    provider_name = "local_hashing"

    TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+")

    def embed_text(self, text):
        vector = [0.0] * self.dimensions

        tokens = self.TOKEN_PATTERN.findall((text or "").lower())

        if not tokens:
            return vector

        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign

        norm = math.sqrt(sum(value * value for value in vector))

        if norm == 0:
            return vector

        return [value / norm for value in vector]


class EmbeddingProviderFactory:
    @staticmethod
    def get_provider(provider_name="local_hashing"):
        if provider_name == "local_hashing":
            return LocalHashingEmbeddingProvider()

        raise ValueError(f"Unsupported embedding provider: {provider_name}")
