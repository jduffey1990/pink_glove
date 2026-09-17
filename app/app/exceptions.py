"""
Typed API exceptions.

Raising these keeps status codes out of view bodies and gives DRF's exception
handler a consistent JSON shape to render.

`ConflictError` is the one services raise. It lives here, next to the handler
that renders it, so every app -- scheduling, billing -- gets the same 409 from
the same raise, and no view needs a try/except to turn one into a response.
"""

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.exceptions import APIException
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.views import exception_handler as drf_exception_handler


class ConflictError(Exception):
    """
    The request was well-formed but conflicts with the record's current state.

    Services raise this; `exception_handler` renders it as a 409. The
    distinction from a 400 matters to the frontend: a 400 means fix the
    payload, a 409 means re-read the record.

    Carries an optional `payload` merged into the response body -- the job
    status action uses it to return the allowed next states, so a client that
    guessed wrong can render the right buttons without a second round trip.
    """

    def __init__(self, detail: str, payload: dict | None = None):
        super().__init__(detail)
        self.detail = detail
        self.payload = payload or {}


def exception_handler(exc, context):
    """
    Translate Django's ValidationError into DRF's, so it renders as a 400, and
    a service's `ConflictError` into a 409.

    DRF only understands its own exception type; a Django ValidationError
    raised from `Model.save()` or `Model.clean()` would otherwise escape as an
    unhandled 500. That matters here because `base.models.TenantModel.save()`
    raises exactly that when a write points at another organization's record --
    a caller error, not a server fault.
    """
    if isinstance(exc, DjangoValidationError):
        detail = exc.message_dict if hasattr(exc, "message_dict") else {"detail": exc.messages}
        exc = DRFValidationError(detail)

    elif isinstance(exc, ConflictError):
        # The payload is merged into the body rather than nested, so a client
        # reads `detail` and `allowed` off the same object it already handles.
        exc = ConflictJSON({"detail": exc.detail, **exc.payload})

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
