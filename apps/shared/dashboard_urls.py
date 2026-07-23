from django.urls import path

from apps.shared.views import (
    DashboardApprovalsAPIView,
    DashboardDocumentsAPIView,
    DashboardExtractionsAPIView,
    DashboardOverviewAPIView,
    DashboardRAGUsageAPIView,
    DashboardRecentActivityAPIView,
)

urlpatterns = [
    path("overview/", DashboardOverviewAPIView.as_view(), name="dashboard-overview"),
    path("documents/", DashboardDocumentsAPIView.as_view(), name="dashboard-documents"),
    path("approvals/", DashboardApprovalsAPIView.as_view(), name="dashboard-approvals"),
    path(
        "extractions/",
        DashboardExtractionsAPIView.as_view(),
        name="dashboard-extractions",
    ),
    path("rag-usage/", DashboardRAGUsageAPIView.as_view(), name="dashboard-rag-usage"),
    path(
        "recent-activity/",
        DashboardRecentActivityAPIView.as_view(),
        name="dashboard-recent-activity",
    ),
]
