"""Lookup filters must narrow existing authorized querysets, never broaden them."""

import pytest
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.tenants.models import Company
from apps.workspaces.models import Department
from apps.workflows.services.compliance_library_service import (
    ComplianceLibraryService as library,
)


@pytest.fixture
def lookup_data(db):
    company = Company.objects.create(name="Lookup", slug="lookup")
    other = Company.objects.create(name="Other", slug="lookup-other")
    admin = User.objects.create_user(
        username="lookup-admin", company=company, role="company_admin"
    )
    foreign = User.objects.create_user(
        username="lookup-foreign", company=other, role="company_admin"
    )
    department = Department.objects.create(company=company, name="Finance")
    foreign_department = Department.objects.create(company=other, name="Finance secret")
    framework = library.create_framework(
        admin,
        code="access",
        name="Access governance",
        draft={"requirements": [{"code": "R1", "title": "Access review"}]},
    )
    version = library.publish(admin, framework, revision=1)
    control = library.create_control(
        admin,
        code="review",
        name="Quarterly access review",
        department=department.pk,
        definition={"objective": "Review access", "owner": {"user": admin.pk}},
    )
    library.map_control(
        admin, control, revision=1, requirement_uuid=version.requirements.get().uuid
    )
    return admin, foreign, department, foreign_department, framework, version, control


@pytest.mark.django_db
def test_department_numeric_resolution_is_tenant_scoped(lookup_data):
    admin, _, department, foreign, *_ = lookup_data
    client = APIClient()
    client.force_authenticate(admin)
    url = "/api/workspaces/departments/"
    response = client.get(url, {"id": department.pk, "search": "Finance"})
    assert response.status_code == 200
    assert response.data[0]["id"] == department.pk
    assert not client.get(url, {"id": foreign.pk}).data
    assert not client.get(url, {"id": "invalid"}).data


@pytest.mark.django_db
def test_framework_search_preserves_tenant_visibility(lookup_data):
    admin, foreign, _, _, framework, *_ = lookup_data
    client = APIClient()
    client.force_authenticate(admin)
    url = "/api/workflows/compliance-frameworks/"
    assert str(framework.uuid) in [
        r["uuid"] for r in client.get(url, {"search": "governance"}).data["results"]
    ]
    assert not client.get(url, {"search": "not-present"}).data["results"]
    client.force_authenticate(foreign)
    assert not client.get(url, {"search": "governance"}).data["results"]


@pytest.mark.django_db
def test_control_search_and_published_version_filter_are_authorized(lookup_data):
    admin, foreign, _, _, _, version, control = lookup_data
    client = APIClient()
    client.force_authenticate(admin)
    url = "/api/workflows/compliance-controls/"
    params = {"search": "Quarterly", "framework_version_uuid": str(version.uuid)}
    response = client.get(url, params)
    assert response.status_code == 200
    assert [r["uuid"] for r in response.data["results"]] == [str(control.uuid)]
    client.force_authenticate(foreign)
    assert not client.get(url, params).data["results"]


@pytest.mark.django_db
def test_lookup_endpoints_require_authentication():
    client = APIClient()
    for path in (
        "workspaces/departments",
        "workflows/compliance-frameworks",
        "workflows/compliance-controls",
    ):
        assert client.get("/api/" + path + "/").status_code in {401, 403}
