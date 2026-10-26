from django.conf import settings
from django.db import models
from django.core.exceptions import ValidationError

from apps.shared.models import BaseModel
from apps.documents.models.document import Document


class DocumentAccessPolicy(BaseModel):
    class AccessLevel(models.TextChoices):
        VIEW = "view", "View"
        COMMENT = "comment", "Comment"
        EDIT = "edit", "Edit"
        REVIEW = "review", "Review"
        APPROVE = "approve", "Approve"
        ADMIN = "admin", "Admin"

    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        related_name="access_policies",
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="document_access_policies",
        null=True,
        blank=True,
    )

    role = models.CharField(max_length=50, blank=True)
    access_level = models.CharField(
        max_length=50,
        choices=AccessLevel.choices,
        default=AccessLevel.VIEW,
    )

    can_download = models.BooleanField(default=False)
    can_export = models.BooleanField(default=False)
    can_use_ai = models.BooleanField(default=True)

    def clean(self):
        from apps.accounts.models import User

        if self.user_id and self.user.company_id != self.document.company_id:
            raise ValidationError("Grant user must belong to the document company.")
        if bool(self.user_id) == bool(self.role):
            raise ValidationError("Specify exactly one grant principal.")
        if self.role and self.role not in User.Role.values:
            raise ValidationError("Unsupported grant role.")

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        target = self.user or self.role or "Unknown"
        return f"{self.document.title} - {target} - {self.access_level}"

    class Meta:
        ordering = ["document", "access_level"]
