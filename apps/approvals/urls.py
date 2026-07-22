from rest_framework.routers import DefaultRouter

from apps.approvals.views import ApprovalRequestViewSet

router = DefaultRouter()
router.register("", ApprovalRequestViewSet, basename="approvals")

urlpatterns = router.urls
