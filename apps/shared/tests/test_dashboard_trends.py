from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.documents.models import Document
from apps.shared.services.dashboard_analytics_service import DashboardAnalyticsService
from apps.tenants.models import Company


@pytest.mark.django_db
def test_upload_trend_is_bounded_chronological_and_tenant_isolated():
    company = Company.objects.create(name="Trend", slug="trend-tests")
    other = Company.objects.create(name="Other", slug="trend-other")
    admin = User.objects.create_user(
        username="trend-admin", company=company, role="company_admin"
    )
    foreign = User.objects.create_user(
        username="trend-other", company=other, role="company_admin"
    )
    for days in (0, 0, 2, 40):
        doc = Document.objects.create(
            company=company, uploaded_by=admin, title="Synthetic document"
        )
        Document.objects.filter(pk=doc.pk).update(
            created_at=timezone.now() - timedelta(days=days)
        )
    Document.objects.create(company=other, uploaded_by=foreign, title="Other tenant")
    values = DashboardAnalyticsService.get_document_metrics(admin)["uploads_by_day"]
    assert len(values) == 30
    assert list(values) == sorted(values)
    assert values[timezone.localdate().isoformat()] == 2
    assert values[(timezone.localdate() - timedelta(days=2)).isoformat()] == 1
    assert sum(values.values()) == 3


@pytest.mark.django_db
def test_upload_trend_empty_scope_has_real_zero_counts():
    company = Company.objects.create(name="Empty", slug="trend-empty")
    admin = User.objects.create_user(
        username="trend-empty", company=company, role="company_admin"
    )
    values = DashboardAnalyticsService.get_document_metrics(admin)["uploads_by_day"]
    assert len(values) == 30
    assert set(values.values()) == {0}
