from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.tenants.models import Company
from apps.workspaces.models import Department


class AdministrationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.one = Company.objects.create(name="One", slug="admin-one")
        cls.two = Company.objects.create(name="Two", slug="admin-two")
        cls.root = User.objects.create_superuser(
            username="root-admin", password="RootAdminPassword2026!"
        )
        cls.admin = User.objects.create_user(
            username="one-admin",
            password="CompanyPassword2026!",
            company=cls.one,
            role="company_admin",
        )
        cls.member = User.objects.create_user(
            username="one-member", password="MemberPassword2026!", company=cls.one
        )
        cls.other_admin = User.objects.create_user(
            username="two-admin",
            password="CompanyPassword2026!",
            company=cls.two,
            role="company_admin",
        )

    def setUp(self):
        self.client = APIClient()

    def test_superuser_can_create_company_and_user_without_personal_company(self):
        self.client.force_authenticate(self.root)
        profile = self.client.get("/api/auth/profile/")
        self.assertTrue(profile.data["is_superuser"])
        response = self.client.post("/api/auth/companies/", {"name": "Three"})
        self.assertEqual(response.status_code, 201)
        company_id = response.data["id"]
        response = self.client.post(
            "/api/auth/users/",
            {
                "company": company_id,
                "username": "three-admin",
                "role": "company_admin",
                "password": "StrongCompanyPassword2026!",
            },
        )
        self.assertEqual(response.status_code, 201)
        user = User.objects.get(username="three-admin")
        self.assertEqual(user.company_id, company_id)
        self.assertFalse(user.is_superuser)
        self.assertTrue(user.check_password("StrongCompanyPassword2026!"))
        self.assertNotIn("password", response.data)

    def test_company_admin_is_scoped_and_cannot_create_companies_or_platform_admin(
        self,
    ):
        self.client.force_authenticate(self.admin)
        rows = self.client.get("/api/auth/companies/").data
        self.assertEqual([row["id"] for row in rows], [self.one.pk])
        self.assertEqual(
            self.client.post("/api/auth/companies/", {"name": "Forbidden"}).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(f"/api/auth/companies/{self.two.uuid}/").status_code, 404
        )
        self.assertEqual(
            self.client.get(f"/api/auth/users/{self.other_admin.pk}/").status_code, 404
        )
        self.assertEqual(
            self.client.post(
                "/api/auth/users/",
                {
                    "company": self.two.pk,
                    "username": "bad-tenant",
                    "password": "StrongCompanyPassword2026!",
                },
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                "/api/auth/users/",
                {
                    "company": self.one.pk,
                    "username": "bad-role",
                    "role": "platform_admin",
                    "password": "StrongCompanyPassword2026!",
                },
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.patch(
                f"/api/auth/companies/{self.one.uuid}/", {"status": "suspended"}
            ).status_code,
            400,
        )

    def test_member_cannot_manage_accounts_or_departments(self):
        self.client.force_authenticate(self.member)
        self.assertEqual(self.client.get("/api/auth/users/").status_code, 403)
        self.assertEqual(
            self.client.post(
                "/api/auth/users/",
                {
                    "company": self.one.pk,
                    "username": "blocked",
                    "password": "StrongCompanyPassword2026!",
                },
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/api/workspaces/departments/", {"name": "Blocked"}
            ).status_code,
            403,
        )

    def test_admin_cannot_disable_or_demote_self(self):
        self.client.force_authenticate(self.admin)
        self.assertEqual(
            self.client.patch(
                f"/api/auth/users/{self.admin.pk}/", {"is_active": False}
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.patch(
                f"/api/auth/users/{self.admin.pk}/", {"role": "member"}
            ).status_code,
            400,
        )

    def test_superuser_department_scope_is_explicit(self):
        self.client.force_authenticate(self.root)
        response = self.client.post(
            f"/api/workspaces/departments/?company={self.one.pk}", {"name": "Finance"}
        )
        self.assertEqual(response.status_code, 201)
        department = Department.objects.get(uuid=response.data["uuid"])
        self.assertEqual(department.company_id, self.one.pk)
        self.assertEqual(
            self.client.get(f"/api/workspaces/departments/?company={self.two.pk}").data,
            [],
        )
        self.assertEqual(
            self.client.get("/api/workspaces/departments/").status_code, 403
        )

    def test_superuser_membership_stays_in_selected_company(self):
        department = Department.objects.create(company=self.one, name="Legal")
        self.client.force_authenticate(self.root)
        response = self.client.post(
            f"/api/workspaces/memberships/?company={self.one.pk}",
            {"department": department.pk, "user": self.member.pk, "role": "member"},
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            self.client.post(
                f"/api/workspaces/memberships/?company={self.two.pk}",
                {
                    "department": department.pk,
                    "user": self.other_admin.pk,
                    "role": "member",
                },
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                f"/api/workspaces/memberships/?company={self.one.pk}",
                {
                    "department": department.pk,
                    "user": self.other_admin.pk,
                    "role": "member",
                },
            ).status_code,
            400,
        )

    def test_tenant_bound_platform_admin_does_not_gain_global_company_creation(self):
        platform_admin = User.objects.create_user(
            username="scoped-platform-admin",
            company=self.one,
            role="platform_admin",
            password="PlatformPassword2026!",
        )
        self.client.force_authenticate(platform_admin)
        self.assertEqual(
            self.client.get("/api/auth/companies/").data[0]["id"], self.one.pk
        )
        self.assertEqual(
            self.client.post("/api/auth/companies/", {"name": "Forbidden"}).status_code,
            403,
        )
