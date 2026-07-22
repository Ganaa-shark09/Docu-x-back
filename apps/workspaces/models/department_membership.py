from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from apps.shared.models import BaseModel
from apps.tenants.models import Company
from apps.workspaces.models.department import Department


class DepartmentMembership(BaseModel):
    class Role(models.TextChoices):
        DEPARTMENT_ADMIN = "department_admin", "Department Admin"
        REVIEWER = "reviewer", "Reviewer"
        MEMBER = "member", "Member"
        VIEWER = "viewer", "Viewer"

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="department_memberships",
    )

    department = models.ForeignKey(
        Department,
        on_delete=models.CASCADE,
        related_name="memberships",
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="department_memberships",
    )

    role = models.CharField(
        max_length=50,
        choices=Role.choices,
        default=Role.MEMBER,
        db_index=True,
    )

    is_active = models.BooleanField(default=True, db_index=True)

    def clean(self):
        if self.department and self.company_id != self.department.company_id:
            raise ValidationError("Department must belong to the same company.")

        if self.user and self.user.company_id != self.company_id:
            raise ValidationError("User must belong to the same company.")

    def save(self, *args, **kwargs):
        if self.department_id and not self.company_id:
            self.company = self.department.company

        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.department.name} - {self.user} - {self.role}"

    class Meta:
        unique_together = ("department", "user")
        ordering = ["company", "department", "user"]
        indexes = [
            models.Index(fields=["company", "department"]),
            models.Index(fields=["company", "user"]),
            models.Index(fields=["department", "role"]),
        ]
