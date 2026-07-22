from rest_framework import serializers

from apps.accounts.models import User
from apps.approvals.models import ApprovalComment, ApprovalRequest
from apps.approvals.services import ApprovalWorkflowService
from apps.documents.models import Document
from apps.documents.services import DocumentPermissionService


class ApprovalCommentSerializer(serializers.ModelSerializer):
    created_by_email = serializers.EmailField(source="created_by.email", read_only=True)
    created_by_username = serializers.CharField(
        source="created_by.username", read_only=True
    )

    class Meta:
        model = ApprovalComment
        fields = [
            "uuid",
            "comment_type",
            "comment",
            "field_path",
            "old_value",
            "new_value",
            "created_by",
            "created_by_email",
            "created_by_username",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "uuid",
            "created_by",
            "created_by_email",
            "created_by_username",
            "created_at",
            "updated_at",
        ]


class ApprovalRequestListSerializer(serializers.ModelSerializer):
    document_uuid = serializers.UUIDField(source="document.uuid", read_only=True)
    document_title = serializers.CharField(source="document.title", read_only=True)
    requested_by_email = serializers.EmailField(
        source="requested_by.email", read_only=True
    )
    assigned_to_email = serializers.EmailField(
        source="assigned_to.email", read_only=True
    )
    reviewed_by_email = serializers.EmailField(
        source="reviewed_by.email", read_only=True
    )

    class Meta:
        model = ApprovalRequest
        fields = [
            "uuid",
            "document_uuid",
            "document_title",
            "review_type",
            "status",
            "priority",
            "title",
            "description",
            "requested_by",
            "requested_by_email",
            "assigned_to",
            "assigned_to_email",
            "reviewed_by",
            "reviewed_by_email",
            "due_at",
            "submitted_at",
            "reviewed_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class ApprovalRequestDetailSerializer(serializers.ModelSerializer):
    document_uuid = serializers.UUIDField(source="document.uuid", read_only=True)
    document_title = serializers.CharField(source="document.title", read_only=True)
    requested_by_email = serializers.EmailField(
        source="requested_by.email", read_only=True
    )
    assigned_to_email = serializers.EmailField(
        source="assigned_to.email", read_only=True
    )
    reviewed_by_email = serializers.EmailField(
        source="reviewed_by.email", read_only=True
    )
    comments = ApprovalCommentSerializer(many=True, read_only=True)

    class Meta:
        model = ApprovalRequest
        fields = [
            "uuid",
            "document",
            "document_uuid",
            "document_title",
            "review_type",
            "status",
            "priority",
            "title",
            "description",
            "submitted_payload",
            "reviewed_payload",
            "reviewer_notes",
            "requested_by",
            "requested_by_email",
            "assigned_to",
            "assigned_to_email",
            "reviewed_by",
            "reviewed_by_email",
            "due_at",
            "submitted_at",
            "reviewed_at",
            "comments",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class ApprovalRequestCreateSerializer(serializers.Serializer):
    document = serializers.UUIDField()
    review_type = serializers.ChoiceField(
        choices=ApprovalRequest.ReviewType.choices,
        default=ApprovalRequest.ReviewType.DOCUMENT_REVIEW,
    )
    priority = serializers.ChoiceField(
        choices=ApprovalRequest.Priority.choices,
        default=ApprovalRequest.Priority.MEDIUM,
    )
    title = serializers.CharField(max_length=255)
    description = serializers.CharField(required=False, allow_blank=True)
    assigned_to = serializers.IntegerField(required=False, allow_null=True)
    due_at = serializers.DateTimeField(required=False, allow_null=True)
    submitted_payload = serializers.JSONField(required=False)

    def validate(self, attrs):
        request = self.context["request"]

        document = Document.objects.filter(
            uuid=attrs["document"],
            company=request.user.company,
            is_deleted=False,
        ).first()

        if not document:
            raise serializers.ValidationError({"document": "Document not found."})

        if not DocumentPermissionService.has_document_access(
            user=request.user,
            document=document,
            required_access="review",
        ):
            raise serializers.ValidationError(
                {"document": "You do not have review access for this document."}
            )

        assigned_to = None

        assigned_to_id = attrs.get("assigned_to")
        if assigned_to_id:
            assigned_to = User.objects.filter(
                id=assigned_to_id,
                company=request.user.company,
                is_active=True,
            ).first()

            if not assigned_to:
                raise serializers.ValidationError(
                    {"assigned_to": "Assigned reviewer not found in your company."}
                )

        attrs["document_obj"] = document
        attrs["assigned_to_obj"] = assigned_to

        return attrs

    def create(self, validated_data):
        request = self.context["request"]

        return ApprovalWorkflowService.create_approval_request(
            user=request.user,
            document=validated_data["document_obj"],
            title=validated_data["title"],
            description=validated_data.get("description", ""),
            review_type=validated_data["review_type"],
            priority=validated_data["priority"],
            assigned_to=validated_data.get("assigned_to_obj"),
            due_at=validated_data.get("due_at"),
            submitted_payload=validated_data.get("submitted_payload", {}),
            ip_address=self.context.get("ip_address"),
            user_agent=self.context.get("user_agent", ""),
        )


class ApprovalRequestUpdateSerializer(serializers.ModelSerializer):
    assigned_to = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(),
        required=False,
        allow_null=True,
    )

    class Meta:
        model = ApprovalRequest
        fields = [
            "title",
            "description",
            "priority",
            "assigned_to",
            "due_at",
            "submitted_payload",
            "status",
        ]

    def validate_assigned_to(self, assigned_to):
        request = self.context["request"]

        if assigned_to and assigned_to.company_id != request.user.company_id:
            raise serializers.ValidationError("Reviewer must belong to your company.")

        return assigned_to

    def validate_status(self, status):
        allowed_manual_statuses = [
            ApprovalRequest.Status.DRAFT,
            ApprovalRequest.Status.PENDING,
            ApprovalRequest.Status.IN_REVIEW,
            ApprovalRequest.Status.CANCELLED,
        ]

        if status not in allowed_manual_statuses:
            raise serializers.ValidationError(
                "Use approve/reject/request-changes actions for review decisions."
            )

        return status


class ApprovalDecisionSerializer(serializers.Serializer):
    reviewer_notes = serializers.CharField(required=False, allow_blank=True)
    reviewed_payload = serializers.JSONField(required=False)


class ApprovalRejectSerializer(serializers.Serializer):
    reviewer_notes = serializers.CharField()


class ApprovalCommentCreateSerializer(serializers.Serializer):
    comment_type = serializers.ChoiceField(
        choices=ApprovalComment.CommentType.choices,
        default=ApprovalComment.CommentType.GENERAL,
    )
    comment = serializers.CharField()
    field_path = serializers.CharField(required=False, allow_blank=True)
    old_value = serializers.CharField(required=False, allow_blank=True)
    new_value = serializers.CharField(required=False, allow_blank=True)
