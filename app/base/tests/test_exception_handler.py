"""
`app.exceptions.exception_handler` -- the one place a service's exception
becomes a response.

Both translations exist so that services can raise plain Python exceptions and
no view needs a try/except: a Django `ValidationError` is a 400, and a
`ConflictError` is a 409 with its payload merged into the body.
"""

import pytest
from django.core.exceptions import ValidationError as DjangoValidationError

from app.exceptions import ConflictError, exception_handler


def _handle(exc):
    return exception_handler(exc, {})


class TestConflictError:
    def test_renders_as_409_with_the_detail(self):
        response = _handle(ConflictError("That invoice is already issued."))

        assert response.status_code == 409
        assert response.data["detail"] == "That invoice is already issued."

    def test_the_payload_is_merged_into_the_body_not_nested(self):
        response = _handle(
            ConflictError("Not from there.", {"status": "complete", "allowed": ["cancelled"]})
        )

        assert response.data["status"] == "complete"
        assert list(response.data["allowed"]) == ["cancelled"]

    def test_no_payload_leaves_just_the_detail(self):
        response = _handle(ConflictError("No."))

        assert set(response.data) == {"detail"}


class TestDjangoValidationError:
    def test_a_field_error_renders_as_400_under_its_field(self):
        response = _handle(DjangoValidationError({"amount_cents": "Too much."}))

        assert response.status_code == 400
        assert [str(m) for m in response.data["amount_cents"]] == ["Too much."]

    def test_a_bare_message_lands_under_detail(self):
        response = _handle(DjangoValidationError("Nope."))

        assert response.status_code == 400
        assert [str(m) for m in response.data["detail"]] == ["Nope."]


@pytest.mark.parametrize("exc", [ValueError("something else"), KeyError("k")])
def test_anything_else_is_left_to_drf(exc):
    """An unrecognised exception still escapes as a 500 rather than a 409."""
    assert _handle(exc) is None
