from rest_framework.routers import DefaultRouter

from apps.workspaces.views import DepartmentMembershipViewSet, DepartmentViewSet

router = DefaultRouter()
router.register("departments", DepartmentViewSet, basename="departments")
router.register(
    "memberships", DepartmentMembershipViewSet, basename="department-memberships"
)

urlpatterns = router.urls
