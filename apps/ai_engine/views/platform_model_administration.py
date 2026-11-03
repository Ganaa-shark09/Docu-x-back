"""Superuser-only DOCU-X AI model/provider administration.

Credentials remain server-side, encrypted by LLMProviderConfig.set_api_key.
All endpoints require is_superuser; company administrators cannot change grants or keys.
"""
import logging
import os
from urllib.parse import urlsplit

from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import permissions, serializers, status
from rest_framework.generics import GenericAPIView
from rest_framework.response import Response

from apps.ai_engine.models import LLMProviderConfig
from apps.tenants.models import Company

logger = logging.getLogger(__name__)


class IsPlatformSuperuser(permissions.BasePermission):
    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.is_active and user.is_superuser)


class PlatformCompanySerializer(serializers.ModelSerializer):
    class Meta:
        model = Company
        fields = ("uuid", "name", "slug", "status", "allow_cloud_llm", "allow_local_llm")
        read_only_fields = ("uuid", "name", "slug", "status")


class PlatformProviderSerializer(serializers.ModelSerializer):
    company = serializers.SlugRelatedField(slug_field="uuid", queryset=Company.objects.filter(is_deleted=False))
    api_key = serializers.CharField(write_only=True, required=False, allow_blank=True, trim_whitespace=False)
    has_api_key = serializers.SerializerMethodField()

    class Meta:
        model = LLMProviderConfig
        fields = (
            "uuid", "company", "name", "provider_type", "is_active", "model_name",
            "endpoint_url", "deployment_name", "api_version", "max_tokens",
            "temperature", "api_key", "has_api_key", "created_at", "updated_at",
        )
        read_only_fields = ("uuid", "has_api_key", "created_at", "updated_at")

    def get_has_api_key(self, obj):
        return bool(obj.api_key_encrypted)

    def validate(self, attrs):
        current = self.instance
        provider = attrs.get("provider_type", current.provider_type if current else None)
        company = attrs.get("company", current.company if current else None)
        endpoint = attrs.get("endpoint_url", current.endpoint_url if current else "")
        deployment = attrs.get("deployment_name", current.deployment_name if current else "")
        active = attrs.get("is_active", current.is_active if current else True)
        api_key = attrs.get("api_key", "")
        model = attrs.get("model_name", current.model_name if current else "")
        if current and "company" in attrs and attrs["company"].pk != current.company_id:
            raise serializers.ValidationError({"company": "The owning company cannot be changed. Create a separate configuration."})
        if current and "provider_type" in attrs and attrs["provider_type"] != current.provider_type:
            raise serializers.ValidationError({"provider_type": "Provider type is immutable. Create a new configuration."})
        if not company:
            raise serializers.ValidationError({"company": "Select a company."})
        if provider in ("groq", "gemini", "anthropic", "openai") and endpoint:
            raise serializers.ValidationError({"endpoint_url": "This provider uses a fixed official endpoint; leave this field empty."})
        if endpoint:
            parts = urlsplit(endpoint)
            if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
                raise serializers.ValidationError({"endpoint_url": "Enter a valid HTTP(S) base URL without credentials, query, or fragment."})
            if provider == "ollama":
                allowed_hosts = {
                    name.strip().lower() for name in
                    os.getenv("DOCUX_OLLAMA_ALLOWED_HOSTS", "ollama,localhost,127.0.0.1").split(",") if name.strip()
                }
                if parts.hostname.lower() not in allowed_hosts:
                    raise serializers.ValidationError({"endpoint_url": "Ollama host is not on the server allowlist (DOCUX_OLLAMA_ALLOWED_HOSTS)."})
            if provider == "azure_openai" and parts.scheme != "https":
                raise serializers.ValidationError({"endpoint_url": "Azure OpenAI requires an HTTPS endpoint."})
            if provider != "ollama" and parts.scheme != "https":
                raise serializers.ValidationError({"endpoint_url": "Cloud providers require HTTPS endpoints."})
        if provider == "azure_openai" and active and (not endpoint or not deployment):
            raise serializers.ValidationError({"deployment_name": "Azure OpenAI requires an HTTPS endpoint and deployment name."})
        # Provider configs may be staged inactive before adding their credentials.
        if provider in ("groq", "gemini", "anthropic", "openai", "azure_openai") and active and not api_key and not (current and current.api_key_encrypted):
            raise serializers.ValidationError({"api_key": "An active cloud provider requires an API key."})
        if provider == "local" and model and model != "local-extractive-rag":
            raise serializers.ValidationError({"model_name": "The local fallback supports local-extractive-rag only."})
        return attrs

    def create(self, validated_data):
        api_key = validated_data.pop("api_key", "")
        provider = LLMProviderConfig(**validated_data)
        if api_key:
            provider.set_api_key(api_key)
        provider.save()
        return provider

    def update(self, instance, validated_data):
        api_key = validated_data.pop("api_key", "")
        for field, value in validated_data.items():
            setattr(instance, field, value)
        if api_key:
            instance.set_api_key(api_key)
        instance.save()
        return instance


class PlatformCompanyListAPIView(GenericAPIView):
    permission_classes = [IsPlatformSuperuser]
    serializer_class = PlatformCompanySerializer

    def get(self, request):
        # Bounded pages; the frontend follows validated pagination links.
        from rest_framework.pagination import PageNumberPagination
        pager = PageNumberPagination()
        pager.page_size = 100
        companies = Company.objects.filter(is_deleted=False).order_by("name", "pk")
        page = pager.paginate_queryset(companies, request, view=self)
        return pager.get_paginated_response(self.get_serializer(page, many=True).data)


class PlatformCompanyDetailAPIView(GenericAPIView):
    permission_classes = [IsPlatformSuperuser]
    serializer_class = PlatformCompanySerializer

    def patch(self, request, uuid):
        company = get_object_or_404(Company, uuid=uuid, is_deleted=False)
        serializer = self.get_serializer(company, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        logger.info("platform_ai_company_policy_updated actor=%s company=%s", request.user.pk, company.uuid)
        return Response(serializer.data)


class PlatformProviderListAPIView(GenericAPIView):
    permission_classes = [IsPlatformSuperuser]
    serializer_class = PlatformProviderSerializer

    def get(self, request):
        company_uuid = request.query_params.get("company")
        if not company_uuid:
            raise serializers.ValidationError({"company": "Select a company."})
        company = get_object_or_404(Company, uuid=company_uuid, is_deleted=False)
        rows = LLMProviderConfig.objects.filter(company=company, is_deleted=False).order_by("name", "pk")
        # Key status only. Encrypted key and decrypted value are never serialized.
        return Response(self.get_serializer(rows, many=True).data)

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            obj = serializer.save()
        logger.info("platform_ai_provider_created actor=%s company=%s provider=%s", request.user.pk, obj.company_id, obj.pk)
        return Response(self.get_serializer(obj).data, status=status.HTTP_201_CREATED)


class PlatformProviderDetailAPIView(GenericAPIView):
    permission_classes = [IsPlatformSuperuser]
    serializer_class = PlatformProviderSerializer

    def patch(self, request, uuid):
        obj = get_object_or_404(LLMProviderConfig, uuid=uuid, is_deleted=False)
        serializer = self.get_serializer(obj, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            obj = serializer.save()
        logger.info("platform_ai_provider_updated actor=%s company=%s provider=%s", request.user.pk, obj.company_id, obj.pk)
        return Response(self.get_serializer(obj).data)
