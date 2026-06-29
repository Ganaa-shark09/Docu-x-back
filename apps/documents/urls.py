from rest_framework.routers import DefaultRouter

from apps.documents.views import DocumentViewSet

router = DefaultRouter()
router.register("", DocumentViewSet, basename="documents")

urlpatterns = router.urls
