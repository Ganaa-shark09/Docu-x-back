from rest_framework import serializers


class DashboardRecentActivityQuerySerializer(serializers.Serializer):
    limit = serializers.IntegerField(
        required=False, min_value=1, max_value=100, default=20
    )
