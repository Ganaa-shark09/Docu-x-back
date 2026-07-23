from rest_framework.routers import DefaultRouter

from apps.workflows.views import ExtractionRunViewSet, ExtractionTemplateViewSet

router = DefaultRouter()
router.register(
    "extraction-templates", ExtractionTemplateViewSet, basename="extraction-templates"
)
router.register("extraction-runs", ExtractionRunViewSet, basename="extraction-runs")

urlpatterns = router.urls
