from django.db import models


class DemoDataset(models.Model):
    """Ownership ledger for explicitly enabled, local-only synthetic tooling."""

    key = models.CharField(max_length=80, unique=True)
    inventory = models.JSONField(default=dict)
    references = models.JSONField(default=dict)
    stages = models.JSONField(default=list)
    unavailable = models.JSONField(default=dict)
    updated_at = models.DateTimeField(auto_now=True)
