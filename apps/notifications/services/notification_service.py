from apps.notifications.models import Notification


class NotificationService:
    @classmethod
    def create_notification(
        cls,
        company,
        recipient,
        title,
        message,
        notification_type=Notification.NotificationType.SYSTEM,
        priority=Notification.Priority.NORMAL,
        actor=None,
        resource_type="",
        resource_uuid=None,
        action_url="",
        metadata=None,
    ):
        if not recipient:
            return None

        if recipient.company_id != company.id:
            return None

        return Notification.objects.create(
            company=company,
            recipient=recipient,
            actor=actor,
            notification_type=notification_type,
            priority=priority,
            title=title,
            message=message,
            resource_type=resource_type,
            resource_uuid=resource_uuid,
            action_url=action_url,
            metadata=metadata or {},
        )

    @classmethod
    def notify_approval_assigned(cls, approval_request, actor=None):
        if not approval_request.assigned_to:
            return None

        return cls.create_notification(
            company=approval_request.company,
            recipient=approval_request.assigned_to,
            actor=actor or approval_request.requested_by,
            notification_type=Notification.NotificationType.APPROVAL_ASSIGNED,
            priority=cls._priority_from_approval(approval_request),
            title="Approval request assigned",
            message=(
                f"You have been assigned to review '{approval_request.title}' "
                f"for document '{approval_request.document.title}'."
            ),
            resource_type="approval_request",
            resource_uuid=approval_request.uuid,
            action_url=f"/approvals/{approval_request.uuid}",
            metadata={
                "approval_uuid": str(approval_request.uuid),
                "document_uuid": str(approval_request.document.uuid),
                "document_title": approval_request.document.title,
                "review_type": approval_request.review_type,
                "status": approval_request.status,
            },
        )

    @classmethod
    def notify_approval_approved(cls, approval_request, actor=None):
        return cls._notify_requester(
            approval_request=approval_request,
            actor=actor or approval_request.reviewed_by,
            notification_type=Notification.NotificationType.APPROVAL_APPROVED,
            title="Approval request approved",
            message=(
                f"Your approval request '{approval_request.title}' "
                f"has been approved."
            ),
        )

    @classmethod
    def notify_approval_rejected(cls, approval_request, actor=None):
        return cls._notify_requester(
            approval_request=approval_request,
            actor=actor or approval_request.reviewed_by,
            notification_type=Notification.NotificationType.APPROVAL_REJECTED,
            priority=Notification.Priority.HIGH,
            title="Approval request rejected",
            message=(
                f"Your approval request '{approval_request.title}' "
                f"has been rejected."
            ),
        )

    @classmethod
    def notify_approval_changes_requested(cls, approval_request, actor=None):
        return cls._notify_requester(
            approval_request=approval_request,
            actor=actor or approval_request.reviewed_by,
            notification_type=Notification.NotificationType.APPROVAL_CHANGES_REQUESTED,
            priority=Notification.Priority.HIGH,
            title="Changes requested",
            message=(
                f"Changes were requested for approval request "
                f"'{approval_request.title}'."
            ),
        )

    @classmethod
    def notify_approval_comment_added(cls, approval_request, comment, actor=None):
        recipients = []

        if approval_request.requested_by:
            recipients.append(approval_request.requested_by)

        if approval_request.assigned_to:
            recipients.append(approval_request.assigned_to)

        unique_recipients = {
            recipient.id: recipient
            for recipient in recipients
            if recipient and (not actor or recipient.id != actor.id)
        }

        notifications = []

        for recipient in unique_recipients.values():
            notifications.append(
                cls.create_notification(
                    company=approval_request.company,
                    recipient=recipient,
                    actor=actor,
                    notification_type=Notification.NotificationType.APPROVAL_COMMENT_ADDED,
                    priority=Notification.Priority.NORMAL,
                    title="Approval comment added",
                    message=(
                        f"A comment was added on approval request "
                        f"'{approval_request.title}'."
                    ),
                    resource_type="approval_request",
                    resource_uuid=approval_request.uuid,
                    action_url=f"/approvals/{approval_request.uuid}",
                    metadata={
                        "approval_uuid": str(approval_request.uuid),
                        "comment_uuid": str(comment.uuid),
                        "comment_type": comment.comment_type,
                    },
                )
            )

        return notifications

    @classmethod
    def _notify_requester(
        cls,
        approval_request,
        actor,
        notification_type,
        title,
        message,
        priority=Notification.Priority.NORMAL,
    ):
        if not approval_request.requested_by:
            return None

        if actor and approval_request.requested_by_id == actor.id:
            return None

        return cls.create_notification(
            company=approval_request.company,
            recipient=approval_request.requested_by,
            actor=actor,
            notification_type=notification_type,
            priority=priority,
            title=title,
            message=message,
            resource_type="approval_request",
            resource_uuid=approval_request.uuid,
            action_url=f"/approvals/{approval_request.uuid}",
            metadata={
                "approval_uuid": str(approval_request.uuid),
                "document_uuid": str(approval_request.document.uuid),
                "document_title": approval_request.document.title,
                "review_type": approval_request.review_type,
                "status": approval_request.status,
                "reviewer_notes": approval_request.reviewer_notes,
            },
        )

    @staticmethod
    def _priority_from_approval(approval_request):
        if approval_request.priority == "urgent":
            return Notification.Priority.URGENT

        if approval_request.priority == "high":
            return Notification.Priority.HIGH

        if approval_request.priority == "low":
            return Notification.Priority.LOW

        return Notification.Priority.NORMAL
