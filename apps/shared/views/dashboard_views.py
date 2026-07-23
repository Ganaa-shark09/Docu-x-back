from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.shared.serializers import DashboardRecentActivityQuerySerializer
from apps.shared.services import DashboardAnalyticsService


class DashboardBaseAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def ensure_company_user(self, request):
        if not getattr(request.user, "company_id", None):
            return Response(
                {"detail": "User is not assigned to a company."},
                status=status.HTTP_403_FORBIDDEN,
            )

        return None


class DashboardOverviewAPIView(DashboardBaseAPIView):
    def get(self, request):
        error_response = self.ensure_company_user(request)
        if error_response:
            return error_response

        return Response(DashboardAnalyticsService.get_overview(request.user))


class DashboardDocumentsAPIView(DashboardBaseAPIView):
    def get(self, request):
        error_response = self.ensure_company_user(request)
        if error_response:
            return error_response

        return Response(DashboardAnalyticsService.get_document_metrics(request.user))


class DashboardApprovalsAPIView(DashboardBaseAPIView):
    def get(self, request):
        error_response = self.ensure_company_user(request)
        if error_response:
            return error_response

        return Response(DashboardAnalyticsService.get_approval_metrics(request.user))


class DashboardExtractionsAPIView(DashboardBaseAPIView):
    def get(self, request):
        error_response = self.ensure_company_user(request)
        if error_response:
            return error_response

        return Response(DashboardAnalyticsService.get_extraction_metrics(request.user))


class DashboardRAGUsageAPIView(DashboardBaseAPIView):
    def get(self, request):
        error_response = self.ensure_company_user(request)
        if error_response:
            return error_response

        return Response(DashboardAnalyticsService.get_rag_usage_metrics(request.user))


class DashboardRecentActivityAPIView(DashboardBaseAPIView):
    def get(self, request):
        error_response = self.ensure_company_user(request)
        if error_response:
            return error_response

        serializer = DashboardRecentActivityQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)

        return Response(
            {
                "results": DashboardAnalyticsService.get_recent_activity(
                    user=request.user,
                    limit=serializer.validated_data.get("limit", 20),
                )
            }
        )
