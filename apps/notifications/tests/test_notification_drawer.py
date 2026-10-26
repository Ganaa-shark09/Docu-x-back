import pytest
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.tenants.models import Company
from apps.notifications.models import Notification


@pytest.fixture
def inbox(db):
    company = Company.objects.create(name="Inbox", slug="inbox-tests")
    other = Company.objects.create(name="Other", slug="inbox-other")
    user = User.objects.create_user(username="inbox-owner", company=company)
    colleague = User.objects.create_user(username="inbox-colleague", company=company)
    foreign = User.objects.create_user(username="inbox-foreign", company=other)
    rows = [
        Notification.objects.create(
            company=company,
            recipient=user,
            notification_type="system",
            title="Inbox item " + str(n),
        )
        for n in range(28)
    ]
    secret = Notification.objects.create(
        company=other,
        recipient=foreign,
        notification_type="system",
        title="Other tenant",
    )
    peer = Notification.objects.create(
        company=company,
        recipient=colleague,
        notification_type="system",
        title="Other recipient",
    )
    client = APIClient()
    client.force_authenticate(user)
    return client, rows, secret, peer


@pytest.mark.django_db
def test_inbox_pagination_preserves_legacy_list_contract(inbox):
    client, *_ = inbox
    legacy = client.get("/api/notifications/")
    assert isinstance(legacy.data, list)
    assert len(legacy.data) == 28
    page = client.get("/api/notifications/", {"page": 1})
    assert page.data["count"] == 28
    assert len(page.data["results"]) == 25
    assert page.data["next"]
    assert len(client.get("/api/notifications/", {"page": 2}).data["results"]) == 3


@pytest.mark.django_db
def test_unread_actions_and_filters_use_real_recipient_state(inbox):
    client, rows, *_ = inbox
    assert client.get("/api/notifications/unread-count/").data["unread_count"] == 28
    assert (
        client.post(f"/api/notifications/{rows[0].uuid}/mark-read/").status_code == 200
    )
    assert (
        client.get("/api/notifications/", {"page": 1, "status": "read"}).data["count"]
        == 1
    )
    assert (
        client.get("/api/notifications/", {"page": 1, "status": "unread"}).data["count"]
        == 27
    )
    assert client.post("/api/notifications/mark-all-read/").data["updated_count"] == 27
    assert client.get("/api/notifications/unread-count/").data["unread_count"] == 0


@pytest.mark.django_db
def test_notification_actions_cannot_cross_recipient_or_tenant(inbox):
    client, _, secret, peer = inbox
    for record in (secret, peer):
        assert (
            client.post(f"/api/notifications/{record.uuid}/mark-read/").status_code
            == 404
        )
        record.refresh_from_db()
        assert record.status == "unread"
