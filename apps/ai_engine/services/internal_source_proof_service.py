"""Version pins for new Internal AI answers and their transitive summaries."""

from rest_framework.exceptions import PermissionDenied, ValidationError


class InternalSourceProofService:
    @staticmethod
    def capture(message):
        if message.conversation.scope != "internal" or message.role != "assistant":
            return {}
        from apps.workflows.services.proof_source_service import ProofSourceService

        pins = {}
        previous = message.conversation.messages.filter(is_deleted=False).order_by(
            "-created_at"
        )[:80]
        for item in previous:
            for key, pin in (item.metadata or {}).get("source_proof_pins", {}).items():
                # Conflicting historical versions must never be silently replaced.
                pins[key] = pin if key not in pins or pins[key] == pin else None
                if len(pins) > 100:
                    return {"proof_scope_exceeded": None}
        if len(message.retrieved_sources or []) > 20:
            return {"proof_scope_exceeded": None}
        if not message.retrieved_sources:
            return pins
        resolver = ProofSourceService(message.conversation.user, "ai")
        for source in message.retrieved_sources[:20]:
            key = str(source.get("document_uuid") or "")
            if not key:
                continue
            try:
                pin = resolver.resolve("document", key)["pin"]
            except (PermissionDenied, ValidationError):
                # Existing records may have governance lineage but no retained
                # file/extraction. Preserve existing visibility rules; canonical
                # proof refuses the absent pin rather than inventing a version.
                continue
            pins[key] = pin if key not in pins or pins[key] == pin else None
            if len(pins) > 100:
                return {"proof_scope_exceeded": None}
        return pins

    @staticmethod
    def visible(message, user, allowed):
        pins = (message.metadata or {}).get("source_proof_pins", {})
        if not pins:
            return True
        from apps.workflows.services.proof_source_service import ProofSourceService

        resolver = getattr(allowed, "proof_resolver", None)
        if resolver is None:
            resolver = ProofSourceService(user, "ai")
            if hasattr(allowed, "proof_resolver"):
                allowed.proof_resolver = resolver
        try:
            return all(
                pin and resolver.resolve("document", key)["pin"] == pin
                for key, pin in pins.items()
            )
        except (PermissionDenied, ValidationError):
            return False
