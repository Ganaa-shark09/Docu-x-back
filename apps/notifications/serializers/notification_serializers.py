from rest_framework import serializers

from apps.notifications.models import Notification


class NotificationSerializer(serializers.ModelSerializer):
    actor_email = serializers.EmailField(source="actor.email", read_only=True)
    actor_username = serializers.CharField(source="actor.username", read_only=True)

    class Meta:
        model = Notification
        fields = [
            "uuid",
            "notification_type",
            "status",
            "priority",
            "title",
            "message",
            "actor",
            "actor_email",
            "actor_username",
            "resource_type",
            "resource_uuid",
            "action_url",
            "metadata",
            "read_at",
            "archived_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class NotificationUnreadCountSerializer(serializers.Serializer):
    unread_count = serializers.IntegerField()
