from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema
from rest_framework import filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.viewsets import ReadOnlyModelViewSet

from audit.models import AccessReveal
from audit.serializers import AccessRevealSerializer, ReviewSerializer
from audit.services import mark_reviewed
from base.permissions import IsAdminOrHigher
from base.serializers import DetailSerializer
from base.viewsets import TenantViewSetMixin


class AccessRevealViewSet(TenantViewSetMixin, ReadOnlyModelViewSet):
    """
    The audit trail. Read-only by construction -- there is no update or destroy
    route, and the model refuses deletion.

    Admin and owner only: this is the record used to ask a worker why they
    pulled a code at 11pm, so the people being recorded should not be able to
    curate it.
    """

    queryset = AccessReveal.objects.select_related(
        "user", "location", "location__customer", "reviewed_by"
    )
    serializer_class = AccessRevealSerializer
    permission_classes = [IsAdminOrHigher]

    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["user", "location", "is_flagged", "acknowledged"]
    ordering_fields = ["created_at", "is_flagged"]

    @extend_schema(
        request=ReviewSerializer,
        responses={200: AccessRevealSerializer, 403: DetailSerializer, 409: DetailSerializer},
        summary="Mark a flagged reveal as looked at",
    )
    @action(detail=True, methods=["post"])
    def review(self, request, pk=None):
        """
        Mark a flagged reveal as looked at.

        Closing a flag records who closed it and what they were told. Nothing
        about the original reveal is altered.
        """
        reveal = self.get_object()
        serializer = ReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        membership = getattr(request, "membership", None)
        reveal = mark_reviewed(
            reveal=reveal,
            reviewer=request.user,
            role=membership.role if membership else None,
            note=serializer.validated_data["review_note"],
        )

        return Response(AccessRevealSerializer(reveal, context={"request": request}).data)
