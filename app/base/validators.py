"""Validators shared across apps."""

from django.core.exceptions import ValidationError
from django.utils.deconstruct import deconstructible


@deconstructible
class MaxFileSize:
    """
    Refuse an upload over `megabytes`.

    On the model field rather than a serializer so the API, the admin and
    anything written later all get it. Without a cap, one phone set to its
    highest quality fills the media volume a visit at a time, and the request
    that carries a 200 MB "photo" ties up a worker for as long as it uploads.
    """

    def __init__(self, megabytes: int):
        self.megabytes = megabytes

    def __call__(self, file):
        if file.size > self.megabytes * 1024 * 1024:
            raise ValidationError(
                f"This file is {file.size / (1024 * 1024):.1f} MB; the limit is "
                f"{self.megabytes} MB."
            )

    def __eq__(self, other):
        return isinstance(other, MaxFileSize) and other.megabytes == self.megabytes

    def __hash__(self):
        return hash(("MaxFileSize", self.megabytes))


def validate_date_range(date_from, date_to, *, max_days: int) -> None:
    """
    Refuse a backwards `date_from`/`date_to` pair, or one wider than `max_days`.

    Both dates are organization-local and inclusive. Either may be None, in
    which case there is no range to judge. The error names `date_to`, so a
    form can put it beside the field.

    Here because the jobs filter and the reports ask the same question, and
    the second copy of a rule like this is the one that drifts.
    """
    if date_from is None or date_to is None:
        return

    if date_to < date_from:
        raise ValidationError({"date_to": "date_to cannot fall before date_from."})

    if (date_to - date_from).days > max_days:
        raise ValidationError(
            {"date_to": f"That range covers more than {max_days} days. Ask for a shorter window."}
        )
