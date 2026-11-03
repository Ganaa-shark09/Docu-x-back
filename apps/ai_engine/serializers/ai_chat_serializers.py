from rest_framework import serializers

from apps.ai_engine.models import AIConversation, AIMessage


class AIMessageSerializer(serializers.ModelSerializer):
    def to_representation(self, instance):
        from apps.ai_engine.services.message_visibility_service import (
            MessageVisibilityService,
        )

        request = self.context.get("request")
        if request:
            key = (request.user.pk, instance.conversation.scope)
            cache = self.context.setdefault("message_visibility", {})
            if key not in cache:
                cache[key] = MessageVisibilityService.allowed_documents(
                    request.user, instance.conversation.scope
                )
            if not MessageVisibilityService.visible(instance, request.user, cache[key]):
                return {
                    "uuid": str(instance.uuid),
                    "role": instance.role,
                    "content": MessageVisibilityService.OMITTED,
                    "retrieved_sources": [],
                    "metadata": {"protected_context_omitted": True},
                    "created_at": instance.created_at,
                }
        return super().to_representation(instance)

    class Meta:
        model = AIMessage
        fields = [
            "uuid",
            "role",
            "content",
            "retrieved_sources",
            "model_provider",
            "model_name",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "metadata",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class AIConversationListSerializer(serializers.ModelSerializer):
    department_name = serializers.CharField(source="department.name", read_only=True)

    class Meta:
        model = AIConversation
        fields = [
            "uuid",
            "title",
            "department",
            "department_name",
            "status",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class AIConversationDetailSerializer(serializers.ModelSerializer):
    department_name = serializers.CharField(source="department.name", read_only=True)
    messages = AIMessageSerializer(many=True, read_only=True)

    class Meta:
        model = AIConversation
        fields = [
            "uuid",
            "title",
            "department",
            "department_name",
            "status",
            "metadata",
            "messages",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class AIChatRequestSerializer(serializers.Serializer):
    domain = serializers.ChoiceField(choices=["hr_policy"], required=False)
    policy_context = serializers.JSONField(required=False)
    idempotency_key = serializers.CharField(max_length=128, required=False)
    DOCUMENT_SCOPE_ALL = "all"
    DOCUMENT_SCOPE_SELECTED = "selected"

    message = serializers.CharField()
    model_uuid = serializers.UUIDField(required=False)
    conversation_uuid = serializers.UUIDField(required=False)
    department = serializers.UUIDField(required=False)
    document_type = serializers.CharField(required=False)
    sensitivity_label = serializers.CharField(required=False)
    document_scope = serializers.ChoiceField(
        choices=[DOCUMENT_SCOPE_ALL, DOCUMENT_SCOPE_SELECTED],
        required=False,
        default=DOCUMENT_SCOPE_ALL,
    )
    document_uuids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        allow_empty=True,
        max_length=50,
        default=list,
    )
    top_k = serializers.IntegerField(
        required=False, min_value=1, max_value=20, default=5
    )

    def validate(self, attrs):
        document_uuids = attrs.get("document_uuids", [])

        # Preserve order while preventing duplicate document filters.
        deduplicated = list(dict.fromkeys(document_uuids))
        attrs["document_uuids"] = deduplicated

        # Ergonomic/backward-compatible behavior: if UUIDs are supplied and the
        # client omitted document_scope, infer an explicitly selected scope.
        if deduplicated and "document_scope" not in self.initial_data:
            attrs["document_scope"] = self.DOCUMENT_SCOPE_SELECTED

        document_scope = attrs.get("document_scope", self.DOCUMENT_SCOPE_ALL)

        if document_scope == self.DOCUMENT_SCOPE_SELECTED and not deduplicated:
            raise serializers.ValidationError(
                {
                    "document_uuids": (
                        "At least one document UUID is required when "
                        "document_scope is 'selected'."
                    )
                }
            )

        if document_scope == self.DOCUMENT_SCOPE_ALL and deduplicated:
            raise serializers.ValidationError(
                {
                    "document_uuids": (
                        "document_uuids must be empty when document_scope is 'all'."
                    )
                }
            )

        return attrs


class AIChatResponseSerializer(serializers.Serializer):
    conversation_uuid = serializers.UUIDField()
    message_uuid = serializers.UUIDField()
    answer = serializers.CharField()
    sources = serializers.ListField()
    document_context = serializers.DictField()
    model_provider = serializers.CharField()
    model_name = serializers.CharField()
    usage = serializers.DictField()
