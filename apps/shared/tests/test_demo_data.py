import secrets
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from apps.shared.models import DemoDataset
from apps.shared.services.demo_data_service import DemoDataService
from apps.tenants.models import Company


@pytest.fixture
def demo_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "config.settings.local")
    monkeypatch.setenv("DOCUX_DEMO_ENABLED", "YES")
    monkeypatch.setenv("DOCUX_DEMO_PASSWORD", secrets.token_urlsafe(32))
    with override_settings(
        DEBUG=True, SETTINGS_MODULE="config.settings.local", MEDIA_ROOT=tmp_path
    ):
        with patch(
            "apps.workflows.services.workflow_engine_service.WorkflowEngineService.enqueue"
        ):
            yield


@pytest.mark.django_db
def test_demo_refuses_default_test_settings():
    with pytest.raises(CommandError):
        DemoDataService().seed()
    assert not DemoDataset.objects.exists()


@pytest.mark.django_db
def test_core_idempotency_and_unknown_resource_reset_refusal(demo_environment):
    service = DemoDataService()
    service.seed()
    first = DemoDataset.objects.get().inventory
    service.seed()
    assert DemoDataset.objects.get().inventory == first
    from apps.accounts.models import User

    User.objects.create_user(
        username="untracked", company=Company.objects.get(slug="docux-meridian-demo")
    )
    with pytest.raises(CommandError, match="ownership changed"):
        service.reset(confirmed=True)
    assert User.objects.filter(username="untracked").exists()


@pytest.mark.django_db
def test_core_reset_needs_second_confirmation_and_preserves_other_tenants(
    demo_environment,
):
    service = DemoDataService()
    outsider = Company.objects.create(name="Unrelated", slug="unrelated-local-test")
    service.seed()
    with pytest.raises(CommandError, match="confirmation"):
        service.reset()
    service.reset(confirmed=True)
    assert Company.objects.filter(pk=outsider.pk).exists()
    assert Company.objects.count() == 1


@pytest.mark.django_db
def test_full_seed_runs_real_domain_services(demo_environment):
    from importlib import import_module
    from django.apps import apps

    for module in (
        "0006_seed_invoice_schema",
        "0008_seed_contract_schema",
        "0010_seed_clause_taxonomy",
        "0012_seed_hr_policy_categories",
    ):
        import_module("apps.workflows.migrations." + module).seed(apps, None)
    service = DemoDataService()
    result = service.seed(full=True)
    assert set(result["stages"]) == {
        "core",
        "documents",
        "workflows",
        "reviewed_inputs",
        "hr",
        "rfp",
        "finance",
        "compliance",
        "proof",
        "scenarios",
    }
    inventory = DemoDataset.objects.get().inventory
    service.seed(full=True)
    assert DemoDataset.objects.get().inventory == inventory
    assert result["counts"]["workflows.invoicerecord"] >= 3
    assert result["counts"]["workflows.financetransaction"] == 120
    from apps.workflows.models import InvoiceRecord, ContractReview

    reviewed = list(
        InvoiceRecord.objects.exclude(normalized={}).filter(
            workflow_run__status="review_required"
        )
    )
    assert any(row.normalized.get("grand_total") for row in reviewed)
    assert any(row.duplicates for row in reviewed)
    assert ContractReview.objects.get().normalized.get("contract_title")
    from apps.workflows.models import FinanceTestResult

    assert FinanceTestResult.objects.filter(
        code="invoice",
        status="review_required",
        evidence__reconciliation__in=["amount_mismatch", "overpayment"],
    ).exists()
    verified = service.verify(full=True)
    assert all(verified["security_checks"].values())
    outsider = Company.objects.create(name="Outside demo", slug="outside-full-demo")
    service.reset(confirmed=True)
    assert Company.objects.filter(pk=outsider.pk).exists()
    assert Company.objects.count() == 1
    assert not DemoDataset.objects.exists()


@pytest.mark.django_db
def test_management_command_requires_explicit_confirmation(demo_environment):
    with pytest.raises(CommandError, match="confirm-demo"):
        call_command("seed_demo", stdout=StringIO())


@pytest.mark.django_db
def test_demo_refuses_missing_password(demo_environment, monkeypatch):
    monkeypatch.delenv("DOCUX_DEMO_PASSWORD")
    with pytest.raises(CommandError, match="PASSWORD"):
        DemoDataService().seed()
    assert not Company.objects.exists()


def test_source_fixture_validation():
    from demo_fixtures.validate_fixtures import validate

    assert validate()["source_pdfs"] == 17
