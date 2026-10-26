"""Feature 11 input contract; runtime routes follow the validated backend milestone."""

from rest_framework import serializers

from .workflow_serializers import StrictSerializer


class ContradictionClaimReferenceSerializer(StrictSerializer):
    claim_uuid = serializers.UUIDField()
    revision = serializers.IntegerField(min_value=1)


class ContradictionPairSerializer(StrictSerializer):
    left = ContradictionClaimReferenceSerializer()
    right = ContradictionClaimReferenceSerializer()
    field = serializers.CharField(max_length=250)
    kind = serializers.ChoiceField(choices=["value", "date", "clause"])
    basis = serializers.ChoiceField(choices=["across_documents", "across_versions"])
    units = serializers.CharField(max_length=80, allow_blank=True, default="")

    def validate(self, data):
        if data["left"] == data["right"]:
            raise serializers.ValidationError("Choose two distinct claim revisions.")
        if (
            data["basis"] == "across_documents"
            and data["left"]["claim_uuid"] == data["right"]["claim_uuid"]
        ):
            raise serializers.ValidationError(
                "Across-document claims must be distinct."
            )
        return data


class ContradictionJobCreateSerializer(StrictSerializer):
    department = serializers.IntegerField(min_value=1, allow_null=True, default=None)
    subject = serializers.CharField(max_length=250)
    pairs = ContradictionPairSerializer(many=True, min_length=1, max_length=20)
    idempotency_key = serializers.CharField(max_length=128)

    def validate_pairs(self, pairs):
        identities = set()
        for pair in pairs:
            sides = tuple(
                sorted(
                    (str(pair[side]["claim_uuid"]), pair[side]["revision"])
                    for side in ("left", "right")
                )
            )
            identity = (
                sides,
                pair["field"],
                pair["kind"],
                pair["basis"],
                pair["units"],
            )
            if identity in identities:
                raise serializers.ValidationError("Duplicate comparison pair.")
            identities.add(identity)
        return pairs


class ContradictionReviewSerializer(StrictSerializer):
    revision = serializers.IntegerField(min_value=1)
    decision = serializers.ChoiceField(
        choices=["confirmed", "dismissed", "resolved", "reopened"]
    )
    reason = serializers.CharField(max_length=2000)
    resolution = serializers.CharField(max_length=2000, allow_blank=True, default="")
    idempotency_key = serializers.CharField(max_length=128)

    def validate(self, data):
        if data["decision"] == "resolved" and not data["resolution"].strip():
            raise serializers.ValidationError("A resolution explanation is required.")
        return data
