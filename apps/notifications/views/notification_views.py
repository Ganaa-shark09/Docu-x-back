from rest_framework import filters, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination

from apps.notifications.models import Notification
from apps.notifications.serializers import NotificationSerializer


class NotificationPagination(PageNumberPagination):
    page_size = 25


class NotificationViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = NotificationSerializer
    permission_classes = [permissions.IsAuthenticated]
    lookup_field = "uuid"
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = [
        "title",
        "message",
        "resource_type",
    ]
    ordering_fields = [
        "created_at",
        "updated_at",
        "priority",
        "status",
        "notification_type",
    ]
    ordering = ["-created_at"]
    pagination_class = NotificationPagination

    def paginate_queryset(self, queryset):
        # Preserve the existing unpaginated contract for older clients. The
        # drawer explicitly requests a page and receives a bounded envelope.
        if "page" not in self.request.query_params:
            return None
        return super().paginate_queryset(queryset)

    def get_queryset(self):
        user = self.request.user

        queryset = Notification.objects.filter(
            company=user.company,
            recipient=user,
            is_deleted=False,
        ).select_related(
            "company",
            "recipient",
            "actor",
        )

        status_value = self.request.query_params.get("status")
        notification_type = self.request.query_params.get("notification_type")
        priority = self.request.query_params.get("priority")
        include_archived = self.request.query_params.get("include_archived")

        if not include_archived or include_archived.lower() not in ["true", "1", "yes"]:
            queryset = queryset.exclude(status=Notification.Status.ARCHIVED)

        if status_value:
            queryset = queryset.filter(status=status_value)

        if notification_type:
            queryset = queryset.filter(notification_type=notification_type)

        if priority:
            queryset = queryset.filter(priority=priority)

        return queryset.order_by("-created_at")

    @action(detail=False, methods=["get"], url_path="unread-count")
    def unread_count(self, request):
        count = (
            self.get_queryset()
            .filter(
                status=Notification.Status.UNREAD,
            )
            .count()
        )

        return Response({"unread_count": count})

    @action(detail=True, methods=["post"], url_path="mark-read")
    def mark_read(self, request, uuid=None):
        notification = self.get_object()
        notification.mark_read()

        serializer = self.get_serializer(notification)
        return Response(serializer.data)

    @action(detail=False, methods=["post"], url_path="mark-all-read")
    def mark_all_read(self, request):
        unread_notifications = self.get_queryset().filter(
            status=Notification.Status.UNREAD,
        )

        count = unread_notifications.count()

        for notification in unread_notifications:
            notification.mark_read()

        return Response(
            {
                "detail": "All unread notifications marked as read.",
                "updated_count": count,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="archive")
    def archive(self, request, uuid=None):
        notification = self.get_object()
        notification.archive()

        serializer = self.get_serializer(notification)
        return Response(serializer.data)

    @action(detail=False, methods=["post"], url_path="archive-read")
    def archive_read(self, request):
        read_notifications = self.get_queryset().filter(
            status=Notification.Status.READ,
        )

        count = read_notifications.count()

        for notification in read_notifications:
            notification.archive()

        return Response(
            {
                "detail": "Read notifications archived.",
                "updated_count": count,
            },
            status=status.HTTP_200_OK,
        )
