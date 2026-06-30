from django.contrib.auth import authenticate
from rest_framework import serializers
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import User


class UserProfileSerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(source="company.name", read_only=True)
    company_slug = serializers.CharField(source="company.slug", read_only=True)

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "email",
            "first_name",
            "last_name",
            "company",
            "company_name",
            "company_slug",
            "role",
            "phone_number",
            "job_title",
            "is_email_verified",
            "is_active",
        ]
        read_only_fields = [
            "id",
            "username",
            "email",
            "company",
            "company_name",
            "company_slug",
            "role",
            "is_email_verified",
            "is_active",
        ]


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField(required=False)
    email = serializers.EmailField(required=False)
    email_or_username = serializers.CharField(required=False)
    password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        request = self.context.get("request")

        login_identifier = (
            attrs.get("email_or_username")
            or attrs.get("username")
            or attrs.get("email")
        )
        password = attrs.get("password")

        if not login_identifier:
            raise serializers.ValidationError(
                {
                    "email_or_username": "This field is required. You can also send username or email."
                }
            )

        if not password:
            raise serializers.ValidationError({"password": "This field is required."})

        user = (
            User.objects.filter(email__iexact=login_identifier).first()
            or User.objects.filter(username__iexact=login_identifier).first()
        )

        if not user:
            raise serializers.ValidationError("Invalid username/email or password.")

        authenticated_user = authenticate(
            request=request,
            username=user.username,
            password=password,
        )

        if not authenticated_user:
            raise serializers.ValidationError("Invalid username/email or password.")

        if not authenticated_user.is_active:
            raise serializers.ValidationError("This account is inactive.")

        refresh = RefreshToken.for_user(authenticated_user)

        return {
            "tokens": {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
            },
            "user": UserProfileSerializer(authenticated_user).data,
        }


class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField()

    def validate(self, attrs):
        self.refresh_token = attrs["refresh"]
        return attrs

    def save(self, **kwargs):
        try:
            token = RefreshToken(self.refresh_token)
            token.blacklist()
        except Exception as exc:
            raise serializers.ValidationError(
                "Invalid or expired refresh token."
            ) from exc
