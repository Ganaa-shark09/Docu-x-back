from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from apps.shared.models import BaseModel


class SensitivityPolicy(BaseModel):
    company = models.ForeignKey("tenants.Company", on_delete=models.CASCADE)
    code = models.SlugField(max_length=50)
    name = models.CharField(max_length=150)
    security_class = models.CharField(max_length=50, default="internal")
    minimum_access = models.CharField(max_length=50, default="view")
    allow_ai = models.BooleanField(default=True)
    allow_export = models.BooleanField(default=True)
    allow_cloud = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "code"], name="sensitivity_company_code"
            )
        ]


class RetentionPolicy(BaseModel):
    company = models.ForeignKey("tenants.Company", on_delete=models.CASCADE)
    name = models.CharField(max_length=150)
    duration_days = models.PositiveIntegerField()
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(duration_days__gte=1), name="retention_positive_days"
            )
        ]


class DocumentFieldPolicy(BaseModel):
    """A protected field is denied unless an applicable rule explicitly allows it.

    Rules without a principal apply to everyone. Explicit denial wins among
    applicable rules; unmatched principals receive only a mask. Raw free text
    is blocked whenever a restricted field cannot be safely separated.
    """

    document = models.ForeignKey(
        "documents.Document", on_delete=models.CASCADE, related_name="field_policies"
    )
    field_key = models.SlugField(max_length=150)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True
    )
    role = models.CharField(max_length=50, blank=True)
    can_view = models.BooleanField(default=False)
    can_use_ai = models.BooleanField(default=False)
    can_export = models.BooleanField(default=False)
    masked_view = models.BooleanField(default=True)

    def clean(self):
        if self.user_id and self.user.company_id != self.document.company_id:
            raise ValidationError(
                "Field principal must belong to the document company."
            )
        if self.user_id and self.role:
            raise ValidationError("Specify one field principal.")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    class Meta:
        indexes = [models.Index(fields=["document", "field_key"])]
