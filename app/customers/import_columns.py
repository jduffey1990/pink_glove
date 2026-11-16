"""
The columns a customer import file may carry (docs/DECISIONS.md ADR-031).

This table is the ONE place a CSV header is tied to a field. The template
download, the required-column check, the unknown-column report and the row
reader are all built from it, so renaming a header, adding a column or
dropping one is an edit here and nowhere else.

It is a best guess: no real export from a business was available when it was
written. Expect to change it when one arrives.

What is deliberately NOT here:

- **Which columns are required, and what a cell may hold.** Those are read off
  `CustomerSerializer` and `ServiceLocationSerializer` at run time, the same
  serializers the normal create path uses, so the rules exist once.
- **`organization` and `user`.** The tenant is stamped from the request
  (invariant 2) and a portal login is never linked by a spreadsheet.

One row is one customer and one service location. The three access columns are
encrypted and write-only (ADR-014): the example cells for them are blank on
purpose, so a template passed around the office never carries a real code.
"""

import re
from dataclasses import dataclass

CUSTOMER = "customer"
LOCATION = "location"

#: The limits on one file. An import is one request and one transaction, so
#: both are bounded: a file that has to be read whole into memory, and a
#: number of rows that fits inside the web worker's timeout with room.
MAX_UPLOAD_MEGABYTES = 1
MAX_ROWS = 2000


@dataclass(frozen=True)
class Column:
    #: The header as the template spells it. Matched after `normalize_header`.
    header: str
    #: Which serializer the cell belongs to: `CUSTOMER` or `LOCATION`.
    target: str
    #: The field on that serializer.
    field: str
    #: The cell in the template's one example row.
    example: str = ""


COLUMNS: tuple[Column, ...] = (
    Column("first_name", CUSTOMER, "first_name", "Sample"),
    Column("last_name", CUSTOMER, "last_name", "Customer"),
    Column("company_name", CUSTOMER, "company_name"),
    Column("email", CUSTOMER, "email", "sample.customer@example.com"),
    Column("phone", CUSTOMER, "phone", "303-555-0100"),
    Column("preferred_contact_method", CUSTOMER, "preferred_contact_method", "email"),
    Column("status", CUSTOMER, "status", "active"),
    Column("source", CUSTOMER, "source", "Referral"),
    Column("customer_notes", CUSTOMER, "notes", "Prefers mornings."),
    Column("billing_line1", CUSTOMER, "billing_line1"),
    Column("billing_line2", CUSTOMER, "billing_line2"),
    Column("billing_city", CUSTOMER, "billing_city"),
    Column("billing_state", CUSTOMER, "billing_state"),
    Column("billing_postal_code", CUSTOMER, "billing_postal_code"),
    Column("location_label", LOCATION, "label", "Home"),
    Column("address_line1", LOCATION, "line1", "100 Example St"),
    Column("address_line2", LOCATION, "line2"),
    Column("city", LOCATION, "city", "Lakewood"),
    Column("state", LOCATION, "state", "CO"),
    Column("postal_code", LOCATION, "postal_code", "80226"),
    Column("square_feet", LOCATION, "square_feet", "1800"),
    Column("bedrooms", LOCATION, "bedrooms", "3"),
    Column("bathrooms", LOCATION, "bathrooms", "2.5"),
    Column("parking_notes", LOCATION, "parking_notes", "Driveway."),
    Column("access_notes", LOCATION, "access_notes", "Side door."),
    Column("gate_code", LOCATION, "gate_code"),
    Column("alarm_code", LOCATION, "alarm_code"),
    Column("key_location", LOCATION, "key_location"),
    Column("has_pets", LOCATION, "has_pets", "yes"),
    Column("pet_notes", LOCATION, "pet_notes", "One friendly dog."),
)

_SEPARATORS = re.compile(r"[\s_\-]+")


def normalize_header(text: str) -> str:
    """
    How a header cell is compared: trimmed, case-insensitive, and with spaces
    and hyphens read as underscores, so "First Name", " first_name " and
    "FIRST-NAME" are one column.
    """
    return _SEPARATORS.sub("_", text.strip().casefold()).strip("_")


BY_HEADER: dict[str, Column] = {normalize_header(column.header): column for column in COLUMNS}
