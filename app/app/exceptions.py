"""
Typed API exceptions.

Raising these keeps status codes out of view bodies and gives DRF's exception
handler a consistent JSON shape to render.
"""

from rest_framework.exceptions import APIException


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
