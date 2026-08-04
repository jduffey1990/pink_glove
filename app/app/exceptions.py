"""
Typed API exceptions.

Raising these keeps status codes out of view bodies and gives DRF's exception
handler a consistent JSON shape to render.
"""

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.exceptions import APIException
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.views import exception_handler as drf_exception_handler


def exception_handler(exc, context):
    """
    Translate Django's ValidationError into DRF's, so it renders as a 400.

    DRF only understands its own exception type; a Django ValidationError
    raised from `Model.save()` or `Model.clean()` would otherwise escape as an
    unhandled 500. That matters here because `base.models.TenantModel.save()`
    raises exactly that when a write points at another organization's record --
    a caller error, not a server fault.
    """
    if isinstance(exc, DjangoValidationError):
        detail = exc.message_dict if hasattr(exc, "message_dict") else {"detail": exc.messages}
        exc = DRFValidationError(detail)

    return drf_exception_handler(exc, context)


class BadRequestJSON(APIException):
    status_code = 400
    default_detail = "Bad Request"
    default_code = "bad_request"


class UnauthorizedJSON(APIException):
    status_code = 401
    default_detail = "Unauthorized"
    default_code = "unauthorized"


class ForbiddenJSON(APIException):
    status_code = 403
    default_detail = "Forbidden"
    default_code = "forbidden"


class ConflictJSON(APIException):
    status_code = 409
    default_detail = "Conflict"
    default_code = "conflict"
