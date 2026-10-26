from rest_framework.routers import DefaultRouter

from apps.documents.views import DocumentViewSet
from apps.documents.views.governance_views import (
    RetentionPolicyViewSet,
    SensitivityPolicyViewSet,
)

router = DefaultRouter()
router.register(
    "sensitivity-policies", SensitivityPolicyViewSet, basename="sensitivity-policies"
)
router.register(
    "retention-policies", RetentionPolicyViewSet, basename="retention-policies"
)
router.register("", DocumentViewSet, basename="documents")

urlpatterns = router.urls
