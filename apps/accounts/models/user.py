from django.contrib.auth.models import AbstractUser
from django.db import models

from apps.tenants.models import Company


class User(AbstractUser):
    class Role(models.TextChoices):
        PLATFORM_ADMIN = "platform_admin", "Platform Admin"
        COMPANY_ADMIN = "company_admin", "Company Admin"
        DEPARTMENT_ADMIN = "department_admin", "Department Admin"
        REVIEWER = "reviewer", "Reviewer"
        MEMBER = "member", "Member"
        VIEWER = "viewer", "Viewer"

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="users",
        null=True,
        blank=True,
    )

    role = models.CharField(
        max_length=50,
        choices=Role.choices,
        default=Role.MEMBER,
        db_index=True,
    )

    phone_number = models.CharField(max_length=30, blank=True)
    job_title = models.CharField(max_length=150, blank=True)

    is_email_verified = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    @property
    def is_platform_admin(self):
        return self.role == self.Role.PLATFORM_ADMIN or self.is_superuser

    @property
    def is_company_admin(self):
        return self.role == self.Role.COMPANY_ADMIN

    def __str__(self):
        return self.email or self.username
