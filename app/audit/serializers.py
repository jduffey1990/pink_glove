from rest_framework import serializers

from audit.models import AccessReveal
from base.viewsets import TenantModelSerializer


class RevealRequestSerializer(serializers.Serializer):
    """
    Body for the reveal action.

    `acknowledged` must be explicitly true. Requiring it server-side means the
    warning is part of the contract rather than a dialog the frontend could
    quietly stop showing, and the log can state that the user saw it.
    """

    acknowledged = serializers.BooleanField()
    job = serializers.UUIDField(
        required=False,
        allow_null=True,
        help_text=(
            "The visit this reveal is for. Dispatcher and above may name one; "
            "a cleaner's is resolved from their assignment and this is ignored."
        ),
    )


class RevealedCodesSerializer(serializers.Serializer):
    """
    What a reveal returns. Describes the response for the schema (ADR-019);
    this is the one read shape in the API that carries the codes.
    """

    gate_code = serializers.CharField(allow_blank=True)
    alarm_code = serializers.CharField(allow_blank=True)
    key_location = serializers.CharField(allow_blank=True)
    reveal_id = serializers.UUIDField()
    job = serializers.UUIDField(allow_null=True)


class AcknowledgementRequiredSerializer(serializers.Serializer):
    detail = serializers.CharField()
    acknowledgement_required = serializers.BooleanField()


class AccessWarningSerializer(serializers.Serializer):
    """
    The acknowledgement copy, served so the frontend never hardcodes it.

    One string, in one place: the words shown to the user and the words the
    audit row claims they were shown cannot drift apart (ADR-016, ADR-023).
    """

    warning = serializers.CharField()


class AccessRevealSerializer(TenantModelSerializer):
    """Read-only view of the trail, for admins reviewing end-of-day flags."""

    user_email = serializers.EmailField(source="user.email", read_only=True)
    user_name = serializers.CharField(source="user.full_name", read_only=True)
    location_label = serializers.CharField(source="location.label", read_only=True)
    customer_name = serializers.CharField(source="location.customer.display_name", read_only=True)
    reviewed_by_email = serializers.EmailField(source="reviewed_by.email", read_only=True)
    job_scheduled_start = serializers.DateTimeField(
        source="job.scheduled_start", read_only=True, allow_null=True
    )

    class Meta(TenantModelSerializer.Meta):
        model = AccessReveal
        fields = (
            "id",
            "created_at",
            "user",
            "user_email",
            "user_name",
            "location",
            "location_label",
            "job",
            "job_scheduled_start",
            "customer_name",
            "fields_revealed",
            "acknowledged",
            "ip_address",
            "user_agent",
            "evaluated_at",
            "is_flagged",
            "flag_reason",
            "reviewed_at",
            "reviewed_by",
            "reviewed_by_email",
            "review_note",
        )
        read_only_fields = fields


class ReviewSerializer(serializers.Serializer):
    """An admin closing out a flagged reveal after asking about it."""

    review_note = serializers.CharField(allow_blank=True, required=False, default="")
