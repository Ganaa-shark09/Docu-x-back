from apps.audit.models import AuditLog


class AuditLogService:
    @classmethod
    def log(
        cls,
        company,
        user,
        event_type,
        action,
        severity=AuditLog.Severity.INFO,
        resource_type="",
        resource_uuid=None,
        ip_address=None,
        user_agent="",
        metadata=None,
    ):
        return AuditLog.objects.create(
            company=company,
            user=user,
            event_type=event_type,
            severity=severity,
            action=action,
            resource_type=resource_type,
            resource_uuid=resource_uuid,
            ip_address=ip_address,
            user_agent=user_agent or "",
            metadata=metadata or {},
        )

    @classmethod
    def log_ai_context_usage(
        cls,
        user,
        conversation,
        assistant_message,
        requested_provider,
        effective_provider,
        enforcement_result,
        retrieved_chunks,
        ip_address=None,
        user_agent="",
    ):
        blocked_count = enforcement_result.get("blocked_count", 0)
        redacted_count = enforcement_result.get("redacted_count", 0)
        forced_local = enforcement_result.get("forced_local", False)

        if blocked_count:
            event_type = AuditLog.EventType.AI_CONTEXT_BLOCKED
            severity = AuditLog.Severity.HIGH
            action = "AI context blocked by data firewall."
        elif redacted_count:
            event_type = AuditLog.EventType.AI_CONTEXT_REDACTED
            severity = AuditLog.Severity.MEDIUM
            action = "AI context redacted before model usage."
        elif forced_local:
            event_type = AuditLog.EventType.AI_PROVIDER_ROUTED
            severity = AuditLog.Severity.MEDIUM
            action = "AI provider routed to local/private mode by data firewall."
        else:
            event_type = AuditLog.EventType.AI_CONTEXT_USED
            severity = AuditLog.Severity.INFO
            action = "AI context used for RAG answer."

        return cls.log(
            company=user.company,
            user=user,
            event_type=event_type,
            severity=severity,
            action=action,
            resource_type="ai_message",
            resource_uuid=assistant_message.uuid,
            ip_address=ip_address,
            user_agent=user_agent,
            metadata={
                "conversation_uuid": str(conversation.uuid),
                "assistant_message_uuid": str(assistant_message.uuid),
                "requested_provider": requested_provider,
                "effective_provider": effective_provider,
                "retrieved_chunk_count": len(retrieved_chunks),
                "allowed_count": enforcement_result.get("allowed_count", 0),
                "blocked_count": blocked_count,
                "redacted_count": redacted_count,
                "forced_local": forced_local,
                "policy_actions": enforcement_result.get("policy_actions", []),
            },
        )
