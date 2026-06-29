from django.db import models
from django.utils.text import slugify

from apps.shared.models import BaseModel
from apps.tenants.models import Company


class Department(BaseModel):
    class DepartmentType(models.TextChoices):
        FINANCE = "finance", "Finance"
        LEGAL = "legal", "Legal"
        HR = "hr", "HR"
        SALES = "sales", "Sales"
        OPERATIONS = "operations", "Operations"
        MANAGEMENT = "management", "Management"
        CUSTOM = "custom", "Custom"

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="departments",
    )

    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, db_index=True)
    department_type = models.CharField(
        max_length=50,
        choices=DepartmentType.choices,
        default=DepartmentType.CUSTOM,
        db_index=True,
    )

    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    default_sensitivity_label = models.CharField(
        max_length=50,
        default="internal",
    )

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.company.name} - {self.name}"

    class Meta:
        unique_together = ("company", "slug")
        ordering = ["company__name", "name"]
