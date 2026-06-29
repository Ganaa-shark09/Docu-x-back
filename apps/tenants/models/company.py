from django.db import models
from django.utils.text import slugify

from apps.shared.models import BaseModel


class Company(BaseModel):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        INACTIVE = "inactive", "Inactive"
        SUSPENDED = "suspended", "Suspended"

    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True, db_index=True)
    legal_name = models.CharField(max_length=255, blank=True)
    website = models.URLField(blank=True)

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE,
        db_index=True,
    )

    country = models.CharField(max_length=100, blank=True)
    timezone = models.CharField(max_length=100, default="UTC")

    data_retention_days = models.PositiveIntegerField(default=365)
    allow_cloud_llm = models.BooleanField(default=True)
    allow_local_llm = models.BooleanField(default=False)
    allow_byok = models.BooleanField(default=False)

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = "Company"
        verbose_name_plural = "Companies"
        ordering = ["name"]
