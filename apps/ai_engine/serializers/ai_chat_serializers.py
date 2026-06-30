from rest_framework import serializers

from apps.ai_engine.models import AIConversation, AIMessage


class AIMessageSerializer(serializers.ModelSerializer):
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
    message = serializers.CharField()
    conversation_uuid = serializers.UUIDField(required=False)
    department = serializers.UUIDField(required=False)
    document_type = serializers.CharField(required=False)
    sensitivity_label = serializers.CharField(required=False)
    top_k = serializers.IntegerField(
        required=False, min_value=1, max_value=20, default=5
    )


class AIChatResponseSerializer(serializers.Serializer):
    conversation_uuid = serializers.UUIDField()
    message_uuid = serializers.UUIDField()
    answer = serializers.CharField()
    sources = serializers.ListField()
    model_provider = serializers.CharField()
    model_name = serializers.CharField()
    usage = serializers.DictField()
