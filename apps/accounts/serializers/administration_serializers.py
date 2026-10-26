from django.contrib.auth.password_validation import validate_password
from django.utils.text import slugify
from rest_framework import serializers

from apps.accounts.models import User
from apps.tenants.models import Company


class ManagedCompanySerializer(serializers.ModelSerializer):
    class Meta:
        model = Company
        fields = (
            "id",
            "uuid",
            "name",
            "slug",
            "legal_name",
            "website",
            "country",
            "timezone",
            "status",
        )
        read_only_fields = ("id", "uuid", "slug")

    def validate(self, attrs):
        if "status" in attrs and not self.context["request"].user.is_superuser:
            raise serializers.ValidationError(
                {"status": "Only a superuser may change company status."}
            )
        return attrs

    def validate_name(self, value):
        if self.instance is None:
            slug = slugify(value)
            if not slug or Company.objects.filter(slug=slug).exists():
                raise serializers.ValidationError("Choose a unique company name.")
        return value


class ManagedUserSerializer(serializers.ModelSerializer):
    company = serializers.PrimaryKeyRelatedField(queryset=Company.objects.none())
    password = serializers.CharField(
        write_only=True, required=False, trim_whitespace=False, min_length=12
    )

    class Meta:
        model = User
        fields = (
            "id",
            "username",
            "email",
            "first_name",
            "last_name",
            "company",
            "role",
            "is_active",
            "password",
        )
        read_only_fields = ("id",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        actor = self.context["request"].user
        self.fields["company"].queryset = Company.objects.filter(is_deleted=False)
        if not actor.is_superuser:
            self.fields["company"].queryset = self.fields["company"].queryset.filter(
                pk=actor.company_id
            )
        if self.instance is not None:
            self.fields["company"].read_only = True

    def validate_password(self, value):
        validate_password(value, self.instance)
        return value

    def validate_username(self, value):
        qs = User.objects.filter(username__iexact=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("Username is already in use.")
        return value

    def validate_email(self, value):
        qs = User.objects.filter(email__iexact=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if value and qs.exists():
            raise serializers.ValidationError("Email is already in use.")
        return value

    def validate(self, attrs):
        actor = self.context["request"].user
        if self.instance is None and not attrs.get("password"):
            raise serializers.ValidationError({"password": "A password is required."})
        company = attrs.get("company") or (
            self.instance.company if self.instance else None
        )
        if not company or company.is_deleted or company.status != Company.Status.ACTIVE:
            raise serializers.ValidationError(
                {"company": "An active company is required."}
            )
        role = attrs.get(
            "role", self.instance.role if self.instance else User.Role.MEMBER
        )
        if role == User.Role.PLATFORM_ADMIN or (
            self.instance and self.instance.is_superuser
        ):
            raise serializers.ValidationError(
                {"role": "Platform accounts cannot be managed here."}
            )
        if not actor.is_superuser and company.pk != actor.company_id:
            raise serializers.ValidationError({"company": "Company access is denied."})
        if self.instance and self.instance.pk == actor.pk:
            if attrs.get("is_active") is False or (
                "role" in attrs and role != actor.role
            ):
                raise serializers.ValidationError(
                    "You cannot remove your own administration access."
                )
        return attrs

    def create(self, validated_data):
        password = validated_data.pop("password")
        return User.objects.create_user(password=password, **validated_data)

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        if password:
            instance.set_password(password)
        instance.save()
        return instance
