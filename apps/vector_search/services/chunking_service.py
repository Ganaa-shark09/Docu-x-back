class ChunkingService:
    DEFAULT_CHUNK_SIZE = 1200
    DEFAULT_OVERLAP = 200

    @classmethod
    def chunk_text(cls, text, chunk_size=None, overlap=None):
        chunk_size = chunk_size or cls.DEFAULT_CHUNK_SIZE
        overlap = overlap or cls.DEFAULT_OVERLAP

        text = (text or "").strip()

        if not text:
            return []

        chunks = []
        start = 0
        text_length = len(text)

        while start < text_length:
            end = min(start + chunk_size, text_length)
            chunk = text[start:end].strip()

            if chunk:
                chunks.append(
                    {
                        "content": chunk,
                        "start_char": start,
                        "end_char": end,
                        "character_count": len(chunk),
                        "token_count": len(chunk.split()),
                    }
                )

            if end >= text_length:
                break

            start = max(end - overlap, start + 1)

        return chunks
