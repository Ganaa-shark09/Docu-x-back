from rest_framework import serializers


class VectorSearchRequestSerializer(serializers.Serializer):
    query = serializers.CharField()
    department = serializers.UUIDField(required=False)
    document_type = serializers.CharField(required=False)
    sensitivity_label = serializers.CharField(required=False)
    top_k = serializers.IntegerField(
        required=False, min_value=1, max_value=50, default=10
    )


class VectorSearchResultSerializer(serializers.Serializer):
    chunk_uuid = serializers.UUIDField()
    document_uuid = serializers.UUIDField()
    document_title = serializers.CharField()
    document_filename = serializers.CharField()
    document_mime_type = serializers.CharField(allow_blank=True)
    department_uuid = serializers.UUIDField(allow_null=True)
    department_name = serializers.CharField(allow_null=True)
    document_type = serializers.CharField()
    sensitivity_label = serializers.CharField()
    page_number = serializers.IntegerField(allow_null=True)
    chunk_index = serializers.IntegerField()
    content = serializers.CharField()
    score = serializers.FloatField()
    distance = serializers.FloatField()
    embedding_provider = serializers.CharField()
    metadata = serializers.DictField()
