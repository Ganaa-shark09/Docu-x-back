from apps.ai_engine.models import LLMProviderConfig
from apps.data_firewall.models import FirewallScan
from apps.data_firewall.services.firewall_scan_service import FirewallScanService
from apps.documents.models import Document
from apps.documents.services import DocumentPermissionService
from apps.documents.services.document_governance_service import (
    DocumentGovernanceService,
)


class AIContextPolicyEnforcementService:
    CLOUD_PROVIDERS = {
        LLMProviderConfig.ProviderType.OPENAI,
        LLMProviderConfig.ProviderType.AZURE_OPENAI,
    }

    LOCAL_PRIVATE_PROVIDERS = {
        LLMProviderConfig.ProviderType.LOCAL,
        LLMProviderConfig.ProviderType.OLLAMA,
    }

    LOCAL_ONLY_SENSITIVITY_LABELS = {
        Document.Sensitivity.RESTRICTED,
        Document.Sensitivity.HIGHLY_CONFIDENTIAL,
    }

    REDACT_FOR_CLOUD_LABELS = {
        Document.Sensitivity.CONFIDENTIAL,
    }

    @classmethod
    def enforce(cls, user, provider_config, retrieved_chunks):
        requested_provider_type = (
            provider_config.provider_type
            if provider_config
            else LLMProviderConfig.ProviderType.LOCAL
        )

        is_cloud_provider = requested_provider_type in cls.CLOUD_PROVIDERS
        forced_local = False

        document_uuids = [
            chunk.get("document_uuid")
            for chunk in retrieved_chunks
            if chunk.get("document_uuid")
        ]

        documents = {
            str(document.uuid): document
            for document in Document.objects.filter(
                uuid__in=document_uuids,
                company=user.company,
                is_deleted=False,
            ).select_related("company", "department")
        }
        authorized = {
            str(value)
            for value in DocumentPermissionService.get_accessible_document_queryset(
                user, require_ai=True
            )
            .filter(uuid__in=document_uuids)
            .values_list("uuid", flat=True)
        }

        firewall_scans = {
            str(scan.document.uuid): scan
            for scan in FirewallScan.objects.filter(
                document__uuid__in=document_uuids,
                document__company=user.company,
                document__is_deleted=False,
            ).select_related("document")
        }

        sanitized_chunks = []
        policy_actions = []

        blocked_count = 0
        redacted_count = 0

        for chunk in retrieved_chunks:
            document_uuid = str(chunk.get("document_uuid"))
            document = documents.get(document_uuid)
            scan = firewall_scans.get(document_uuid)

            if not document:
                blocked_count += 1
                policy_actions.append(
                    cls._action(
                        chunk=chunk,
                        action="blocked",
                        reason="document_not_found_or_not_allowed",
                    )
                )
                continue

            if document_uuid not in authorized or (
                chunk.get("governance_revision") is not None
                and chunk["governance_revision"] != document.governance_revision
            ):
                blocked_count += 1
                policy_actions.append(
                    cls._action(
                        chunk=chunk,
                        document=document,
                        action="blocked",
                        reason="current_document_or_field_policy_denies_ai",
                    )
                )
                continue

            governance = DocumentGovernanceService.sensitivity(document)

            if scan and scan.requires_human_approval:
                blocked_count += 1
                policy_actions.append(
                    cls._action(
                        chunk=chunk,
                        document=document,
                        scan=scan,
                        action="blocked",
                        reason="firewall_requires_human_approval",
                    )
                )
                continue

            if scan and scan.decision == FirewallScan.Decision.BLOCK_CLOUD_LLM:
                blocked_count += 1
                policy_actions.append(
                    cls._action(
                        chunk=chunk,
                        document=document,
                        scan=scan,
                        action="blocked",
                        reason="firewall_block_cloud_llm_or_critical_risk",
                    )
                )
                continue

            if is_cloud_provider and not document.company.allow_cloud_llm:
                forced_local = True
                policy_actions.append(
                    cls._action(
                        chunk=chunk,
                        document=document,
                        scan=scan,
                        action="provider_routed_to_local",
                        reason="company_disallows_cloud_llm",
                    )
                )

            if is_cloud_provider and not governance["allow_cloud"]:
                forced_local = True
                policy_actions.append(
                    cls._action(
                        chunk=chunk,
                        document=document,
                        scan=scan,
                        action="provider_routed_to_local",
                        reason="document_sensitivity_requires_local_or_private_model",
                    )
                )

            safe_chunk = dict(chunk)

            should_redact = (
                is_cloud_provider
                and not forced_local
                and (
                    governance["security_class"] in cls.REDACT_FOR_CLOUD_LABELS
                    or (
                        scan and scan.decision == FirewallScan.Decision.REDACT_AND_ALLOW
                    )
                )
            )

            if should_redact:
                safe_chunk["content"] = FirewallScanService.redact_text(
                    safe_chunk.get("content", "")
                )
                redacted_count += 1
                policy_actions.append(
                    cls._action(
                        chunk=chunk,
                        document=document,
                        scan=scan,
                        action="redacted",
                        reason="confidential_or_sensitive_context_for_cloud_llm",
                    )
                )
            else:
                policy_actions.append(
                    cls._action(
                        chunk=chunk,
                        document=document,
                        scan=scan,
                        action="allowed",
                        reason="context_allowed",
                    )
                )

            sanitized_chunks.append(safe_chunk)

        effective_provider_config = None if forced_local else provider_config

        return {
            "chunks": sanitized_chunks,
            "provider_config": effective_provider_config,
            "requested_provider_type": requested_provider_type,
            "effective_provider_type": (
                effective_provider_config.provider_type
                if effective_provider_config
                else LLMProviderConfig.ProviderType.LOCAL
            ),
            "forced_local": forced_local,
            "allowed_count": len(sanitized_chunks),
            "blocked_count": blocked_count,
            "redacted_count": redacted_count,
            "policy_actions": policy_actions,
        }

    @staticmethod
    def _action(chunk, action, reason, document=None, scan=None):
        return {
            "chunk_uuid": chunk.get("chunk_uuid"),
            "document_uuid": chunk.get("document_uuid"),
            "document_title": chunk.get("document_title"),
            "chunk_index": chunk.get("chunk_index"),
            "action": action,
            "reason": reason,
            "sensitivity_label": (
                document.sensitivity_label
                if document
                else chunk.get("sensitivity_label")
            ),
            "firewall_risk_level": scan.risk_level if scan else None,
            "firewall_decision": scan.decision if scan else None,
        }
