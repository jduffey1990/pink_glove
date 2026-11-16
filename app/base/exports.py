"""
CSV export, in one place.

Two things here are easy to get wrong in a view and expensive when they are:

* **Formula injection.** A spreadsheet runs a cell that starts with `=`, `+`,
  `-` or `@` as a formula, and a tab or carriage return in front does not stop
  it. A customer called `=HYPERLINK("http://x","Invoice")` is a link the owner
  clicks inside their own accounts. Every text cell goes through
  `neutralise`, which prefixes such a value with an apostrophe so the
  spreadsheet shows it as the text it is.
* **Money.** Cents become dollars by moving the decimal point of an integer
  (`cents_as_dollars`), never by dividing a float (invariant 5, ADR-009).

Numbers this code computed (`int`, `Decimal`) are written as they are: they
cannot carry a formula, and turning a number into text would break every sum
the reader is about to write. Only text is neutralised, and everything a
person typed arrives here as text.
"""

import csv
import io
from collections.abc import Iterable, Sequence
from decimal import Decimal

from django.http import HttpResponse
from rest_framework.negotiation import BaseContentNegotiation

#: What a spreadsheet takes as the start of a formula. The tab and the
#: carriage return are here because both are skipped before the cell is read.
FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")

#: Excel reads a CSV without this as the machine's legacy code page, and
#: "Renée" arrives as "RenÃ©e". Sheets and Numbers ignore it.
UTF8_BOM = "﻿"


def neutralise(value) -> str | int | Decimal:
    """
    `value` as a CSV cell a spreadsheet will not execute.

    None is an empty cell. A bool is refused rather than guessed at: "True"
    is not a word any of these reports should print, so the caller says what
    it means.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        raise TypeError("Write a bool as the word it means, not as True or False.")
    if isinstance(value, int | Decimal):
        return value

    text = str(value)
    if text.startswith(FORMULA_TRIGGERS):
        return f"'{text}"
    return text


def cents_as_dollars(cents: int) -> Decimal:
    """
    Integer cents as decimal dollars, exactly: 15000 is 150.00 and 5 is 0.05.

    The decimal point moves; nothing is divided, so no float is ever involved.
    A column of these is headed `..._usd`, which is what tells the reader the
    figure is dollars and not the cents the API speaks.
    """
    return Decimal(cents).scaleb(-2)


def csv_response(filename: str, header: Sequence[str], rows: Iterable[Sequence]) -> HttpResponse:
    """
    A CSV download: one header row, then `rows`, every cell neutralised.

    A plain `HttpResponse`, not a DRF `Response`: the body is already bytes
    and no renderer should have a second opinion about it.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")

    writer.writerow([neutralise(cell) for cell in header])
    for row in rows:
        writer.writerow([neutralise(cell) for cell in row])

    response = HttpResponse(
        (UTF8_BOM + buffer.getvalue()).encode("utf-8"),
        content_type="text/csv; charset=utf-8",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


class AnyAcceptNegotiation(BaseContentNegotiation):
    """
    Content negotiation for a view whose success body is a file.

    The default refuses `Accept: text/csv` with a 406, because no renderer on
    the view produces CSV: the view returns the bytes itself. A client asking
    for exactly what the endpoint serves must not be turned away, so the
    header is not consulted, and errors still render as JSON like every other
    endpoint's.
    """

    def select_parser(self, request, parsers):
        return parsers[0]

    def select_renderer(self, request, renderers, format_suffix=None):
        return renderers[0], renderers[0].media_type
