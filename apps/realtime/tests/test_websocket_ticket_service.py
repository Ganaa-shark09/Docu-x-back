from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.realtime.tickets import WebSocketTicketService
from apps.tenants.models import Company


class WebSocketTicketServiceTests(TestCase):
    def test_ticket_contains_only_short_lived_identity_reference(self):
        company = Company.objects.create(name="Realtime Co", slug="realtime-ticket-co")
        user = get_user_model().objects.create_user(
            username="realtime-user",
            email="realtime@example.com",
            password="test-password",
            company=company,
        )

        ticket = WebSocketTicketService.issue(user)
        payload = WebSocketTicketService.verify(ticket)

        self.assertEqual(payload["user_id"], user.pk)
        self.assertEqual(payload["company_uuid"], str(company.uuid))
        self.assertNotIn("password", payload)
        self.assertNotIn("access_token", payload)
