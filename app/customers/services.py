"""
Customer import (docs/DECISIONS.md ADR-031).

A CSV of an existing customer book becomes customers and service locations.
Three steps, kept apart so a dry run can stop after the second:

1. `read_file` turns the upload into rows. Anything wrong with the file as a
   whole (encoding, a missing column, too many rows) is refused here with a
   Django `ValidationError`, which the API renders as a 400.
2. `_plan` decides what each row is: a new customer, a further location, a
   row that is already on file, or an error. It validates every row with the
   serializers the normal create path uses and reads the database, but writes
   nothing. A dry run is exactly this step and no more.
3. `_write` saves the plan. It only runs for a real import whose plan has no
   error row, inside the same transaction that made the plan.

**Access codes.** The gate code, alarm code and key location go from the
file's cell into the serializer and from there into the encrypted model field
(ADR-014). They are never copied into an outcome, a message or a log line:
`RowOutcome` has no field that could hold one, and nothing in this module
logs row content.
"""

import csv
import io
import re
from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.db import transaction
from rest_framework import serializers

from customers.enums import ImportAction
from customers.import_columns import (
    BY_HEADER,
    COLUMNS,
    CUSTOMER,
    LOCATION,
    MAX_ROWS,
    MAX_UPLOAD_MEGABYTES,
    Column,
    normalize_header,
)
from customers.models import Customer, ServiceLocation
from customers.serializers import CustomerSerializer, ImportedLocationSerializer
from organizations.models import Organization

TEMPLATE_FILENAME = "customer-import-template.csv"

_NON_DIGITS = re.compile(r"\D")


# --- the template ------------------------------------------------------------


def _serializer_fields() -> dict[str, dict]:
    """The create path's fields, by target. Where 'required' and the hints come from."""
    return {
        CUSTOMER: CustomerSerializer().fields,
        LOCATION: ImportedLocationSerializer().fields,
    }


def required_headers() -> list[str]:
    """Headers a file cannot do without: the columns whose field the serializer requires."""
    fields = _serializer_fields()
    return [column.header for column in COLUMNS if fields[column.target][column.field].required]


def _hint(serializer_field) -> str:
    if serializer_field.write_only:
        return "Stored encrypted. It is not shown again after the import."
    if isinstance(serializer_field, serializers.ChoiceField):
        return "One of: " + ", ".join(str(choice) for choice in serializer_field.choices) + "."
    if isinstance(serializer_field, serializers.BooleanField):
        return "yes or no."
    return str(serializer_field.help_text or "")


def template() -> dict:
    """
    The header row, one example row, and what each column is.

    Built from `import_columns.COLUMNS` and the serializers, so the file a
    person downloads and the file the importer accepts cannot drift apart.
    """
    fields = _serializer_fields()

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([column.header for column in COLUMNS])
    writer.writerow([column.example for column in COLUMNS])

    return {
        "filename": TEMPLATE_FILENAME,
        "csv": buffer.getvalue(),
        "columns": [
            {
                "header": column.header,
                "required": fields[column.target][column.field].required,
                "hint": _hint(fields[column.target][column.field]),
            }
            for column in COLUMNS
        ],
        "max_rows": MAX_ROWS,
        "max_megabytes": MAX_UPLOAD_MEGABYTES,
    }


# --- reading the file --------------------------------------------------------


@dataclass
class ParsedFile:
    #: One entry per header cell, None where the header is not a known column.
    columns: list[Column | None]
    #: Headers that matched nothing, as the file spelled them. Reported, not imported.
    ignored_columns: list[str]
    #: (spreadsheet row number, trimmed cells) for every row that is not blank.
    rows: list[tuple[int, list[str]]]


def _refuse(message: str):
    return ValidationError({"file": message})


def read_file(upload) -> ParsedFile:
    """
    Decode and split the upload. Raises `ValidationError` for a file that
    cannot be imported at all; a problem with one row is `_plan`'s business.
    """
    try:
        # utf-8-sig reads plain UTF-8 too, and drops the byte order mark Excel
        # writes, which would otherwise glue itself to the first header.
        text = upload.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        raise _refuse(
            "This file is not UTF-8 text. In Excel, save it as "
            '"CSV UTF-8 (Comma delimited)" and upload that.'
        ) from None

    try:
        records = list(csv.reader(io.StringIO(text, newline="")))
    except csv.Error:
        raise _refuse("This file could not be read as CSV.") from None

    header = [cell.strip() for cell in records[0]] if records else []
    while header and not header[-1]:
        header.pop()  # trailing commas, not columns
    if not header:
        raise _refuse("This file is empty.")

    columns: list[Column | None] = []
    ignored: list[str] = []
    repeated: list[str] = []
    for position, cell in enumerate(header, start=1):
        column = BY_HEADER.get(normalize_header(cell))
        if column is None:
            ignored.append(cell or f"(column {position}, no header)")
        elif column in columns and column.header not in repeated:
            repeated.append(column.header)
        columns.append(column)

    if repeated:
        raise _refuse(f"These columns appear more than once: {', '.join(repeated)}.")

    present = {column.header for column in columns if column is not None}
    missing = [name for name in required_headers() if name not in present]
    if missing:
        raise _refuse(
            f"This file is missing required columns: {', '.join(missing)}. "
            "The template has the full list."
        )

    rows = []
    # Numbered as a spreadsheet shows them: the header is row 1. Blank rows
    # are skipped but still counted, so a number always points at the right
    # line of the file the person has open.
    for number, record in enumerate(records[1:], start=2):
        cells = [cell.strip() for cell in record]
        if any(cells):
            rows.append((number, cells))

    if not rows:
        raise _refuse("This file has a header row and nothing under it.")
    if len(rows) > MAX_ROWS:
        raise _refuse(
            f"This file has {len(rows)} rows; the limit is {MAX_ROWS}. "
            "Split it and import each part."
        )

    return ParsedFile(columns=columns, ignored_columns=ignored, rows=rows)


# --- matching (ADR-031) ------------------------------------------------------


def _squash(text: str) -> str:
    return " ".join(str(text or "").split()).casefold()


def customer_key(*, email="", first_name="", last_name="", company_name="", phone="", **_) -> tuple:
    """
    What makes two rows, or a row and a stored customer, the same customer.

    The email when there is one. Without one, the whole name plus the phone
    number's digits. A customer has exactly one key, computed the same way
    from a file row and from the database, so matching does not depend on
    the order rows arrive in: a row with no email never matches a customer
    who has one.
    """
    email = _squash(email)
    if email:
        return ("email", email)
    return (
        "name",
        _squash(first_name),
        _squash(last_name),
        _squash(company_name),
        _NON_DIGITS.sub("", str(phone or "")),
    )


def location_key(*, line1="", line2="", postal_code="", **_) -> tuple:
    """What makes two locations of one customer the same address."""
    return (_squash(line1), _squash(line2), _squash(postal_code))


# --- planning ----------------------------------------------------------------


@dataclass
class _Target:
    """The customer a row lands on: one already stored, or one this file creates."""

    #: Display name, for the sentence shown against later rows.
    name: str
    #: The stored customer. Set at once for an existing one, by `_write` for a new one.
    instance: Customer | None = None
    #: The validated serializer that will create it. None for an existing customer.
    serializer: CustomerSerializer | None = None
    #: The row that creates it. None for an existing customer.
    first_row: int | None = None
    #: Addresses it has or will have, each with the row that adds it (None: stored).
    locations: dict[tuple, int | None] = field(default_factory=dict)


@dataclass
class RowOutcome:
    """
    What happens to one row. The first six fields are the response; there is
    deliberately nowhere here to put a cell's value, access code or otherwise.
    """

    row: int
    action: str
    customer: str
    address: str
    detail: str
    errors: list[dict] = field(default_factory=list)

    # Not serialized: what `_write` needs.
    target: _Target | None = field(default=None, repr=False)
    location: ImportedLocationSerializer | None = field(default=None, repr=False)


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


@dataclass
class ImportResult:
    dry_run: bool
    rows: list[RowOutcome]
    ignored_columns: list[str]
    committed: bool = False

    def _count(self, action: str) -> int:
        return sum(1 for outcome in self.rows if outcome.action == action)

    @property
    def totals(self) -> dict:
        new_customers = self._count(ImportAction.CREATE_CUSTOMER)
        return {
            "rows": len(self.rows),
            "new_customers": new_customers,
            # A new customer always arrives with the location on its row.
            "new_locations": new_customers + self._count(ImportAction.ADD_LOCATION),
            "skipped": self._count(ImportAction.SKIP),
            "errors": self._count(ImportAction.ERROR),
        }

    @property
    def has_errors(self) -> bool:
        return any(outcome.action == ImportAction.ERROR for outcome in self.rows)

    @property
    def can_commit(self) -> bool:
        """A real import of this file would write something. Server-owned (ADR-023)."""
        return not self.has_errors and self.totals["new_locations"] > 0

    @property
    def message(self) -> str:
        totals = self.totals
        errors, skipped = totals["errors"], totals["skipped"]
        made = (
            f"{_plural(totals['new_customers'], 'customer')} and "
            f"{_plural(totals['new_locations'], 'location')}"
        )

        if errors:
            start = "Nothing can be imported yet" if self.dry_run else "Nothing was imported"
            return f"{start}: {_plural(errors, 'row')} {'needs' if errors == 1 else 'need'} fixing."
        if not totals["new_locations"]:
            start = "Nothing to import" if self.dry_run else "Nothing was imported"
            return f"{start}: every row is already on file."
        if self.dry_run:
            text = f"Ready to import {made}. Nothing has been saved yet."
            if skipped:
                text += f" {_plural(skipped, 'row')} already on file will be skipped."
            return text
        text = f"Imported {made}."
        if skipped:
            were = "was" if skipped == 1 else "were"
            text += f" {_plural(skipped, 'row')} already on file {were} skipped."
        return text


def _field_errors(target: str, errors: dict) -> list[dict]:
    """
    A serializer's errors, keyed by the file's header rather than the field's name.

    Every error is carried over, not only those on a mapped column. A rule
    may object to a field the file has no column for; such an error belongs
    to the whole row (a blank column). Dropping it would leave an invalid row
    with no errors, which reads as importable.
    """
    found = []
    mapped = set()
    for column in COLUMNS:
        if column.target != target:
            continue
        mapped.add(column.field)
        found.extend(
            {"column": column.header, "message": str(message)}
            for message in errors.get(column.field, [])
        )
    for name, messages in errors.items():
        if name not in mapped:
            found.extend({"column": "", "message": str(message)} for message in messages)
    return found


def _display(data: dict[str, dict]) -> tuple[str, str]:
    """
    Who and where a row is about, for a person reading the preview.

    Built by the models' own `display_name` and `one_line_address`, from the
    name and address cells only. An unsaved customer with nothing to show
    would fall back to its random id, so that case is blank instead.
    """
    customer = data[CUSTOMER]
    identity = {
        name: customer.get(name, "")
        for name in ("first_name", "last_name", "company_name", "email")
    }
    name = Customer(**identity).display_name if any(identity.values()) else ""

    location = data[LOCATION]
    address = ServiceLocation(
        **{
            name: location.get(name, "")
            for name in ("line1", "line2", "city", "state", "postal_code")
        }
    ).one_line_address
    return name, address


def _existing_targets(organization) -> dict[tuple, list[_Target]]:
    """
    The organization's live customers, by key, each with the addresses it has.

    Scoped to the one organization here and nowhere else (ADR-002): matching
    never sees another tenant's customers. Both querysets go through the
    default manager, so a soft-deleted customer or location is not "on file".
    The address columns are read as plain values: the encrypted ones are not
    selected, so nothing is decrypted to plan an import.
    """
    by_id: dict = {}
    by_key: dict[tuple, list[_Target]] = {}
    for customer in Customer.objects.filter(organization=organization).order_by("created_at", "id"):
        target = _Target(name=customer.display_name, instance=customer)
        by_id[customer.id] = target
        by_key.setdefault(customer_key(**customer.__dict__), []).append(target)

    for customer_id, line1, line2, postal_code in ServiceLocation.objects.filter(
        organization=organization
    ).values_list("customer_id", "line1", "line2", "postal_code"):
        target = by_id.get(customer_id)
        if target is not None:
            target.locations[location_key(line1=line1, line2=line2, postal_code=postal_code)] = None

    return by_key


NOT_IMPORTED = "Not imported. Fix this row and upload the file again."


def _plan_row(
    *,
    number: int,
    cells: list[str],
    columns: list[Column | None],
    existing: dict[tuple, list[_Target]],
    created: dict[tuple, _Target],
    context: dict,
) -> RowOutcome:
    data: dict[str, dict] = {CUSTOMER: {}, LOCATION: {}}
    for column, cell in zip(columns, cells, strict=False):
        # A blank cell is "not given", so the field's own default applies.
        if column is not None and cell:
            data[column.target][column.field] = cell

    name, address = _display(data)

    def outcome(action, detail, **more) -> RowOutcome:
        return RowOutcome(
            row=number, action=action, customer=name, address=address, detail=detail, **more
        )

    # The create path's own serializers: every rule a row is held to lives
    # there. Both are run even when the first fails, so one upload reports
    # everything wrong with a row.
    customer = CustomerSerializer(data=data[CUSTOMER], context=context)
    location = ImportedLocationSerializer(data=data[LOCATION], context=context)
    customer.is_valid()
    location.is_valid()

    errors = [
        *_field_errors(CUSTOMER, customer.errors),
        *_field_errors(LOCATION, location.errors),
    ]
    if any(cells[len(columns) :]):
        errors.append(
            {
                "column": "",
                "message": "This row has more cells than the header has columns. "
                "A value with a comma in it must be wrapped in double quotes.",
            }
        )
    if errors:
        return outcome(ImportAction.ERROR, NOT_IMPORTED, errors=errors)

    key = customer_key(**customer.validated_data)
    address_key = location_key(**location.validated_data)
    matches = existing.get(key, [])

    if len(matches) > 1:
        # Two stored customers share this row's key. Attaching the location
        # to either would be a guess, and a guess here puts someone's alarm
        # code on the wrong record.
        return outcome(
            ImportAction.ERROR,
            NOT_IMPORTED,
            errors=[
                {
                    "column": "email" if key[0] == "email" else "",
                    "message": f"{len(matches)} customers here already match this row, so "
                    "there is no telling which one it belongs to. Remove the row and "
                    "add the location by hand.",
                }
            ],
        )

    target = matches[0] if matches else created.get(key)

    if target is None:
        target = _Target(name=name, serializer=customer, first_row=number)
        target.locations[address_key] = number
        created[key] = target
        return outcome(
            ImportAction.CREATE_CUSTOMER,
            "New customer with this location.",
            target=target,
            location=location,
        )

    if address_key in target.locations:
        earlier = target.locations[address_key]
        return outcome(
            ImportAction.SKIP,
            "Already on file: this customer has this address."
            if earlier is None
            else f"Same customer and address as row {earlier}.",
        )

    target.locations[address_key] = number
    return outcome(
        ImportAction.ADD_LOCATION,
        f"New location for {target.name}, who is already a customer."
        if target.first_row is None
        else f"Another location for the customer on row {target.first_row}.",
        target=target,
        location=location,
    )


def _plan(*, organization, parsed: ParsedFile, dry_run: bool, context: dict) -> ImportResult:
    """Decide every row, in file order. Reads the database; writes nothing."""
    existing = _existing_targets(organization)
    #: Customers this file creates, by key. Never overlaps `existing`.
    created: dict[tuple, _Target] = {}

    return ImportResult(
        dry_run=dry_run,
        ignored_columns=parsed.ignored_columns,
        rows=[
            _plan_row(
                number=number,
                cells=cells,
                columns=parsed.columns,
                existing=existing,
                created=created,
                context=context,
            )
            for number, cells in parsed.rows
        ],
    )


# --- writing -----------------------------------------------------------------


def _write(result: ImportResult, organization) -> None:
    """Save the plan, in row order. `organization` is stamped here (invariant 2)."""
    for outcome in result.rows:
        if outcome.action == ImportAction.CREATE_CUSTOMER:
            outcome.target.instance = outcome.target.serializer.save(organization=organization)
        if outcome.action in (ImportAction.CREATE_CUSTOMER, ImportAction.ADD_LOCATION):
            outcome.location.save(organization=organization, customer=outcome.target.instance)


def import_customers(*, organization, upload, dry_run: bool, context: dict) -> ImportResult:
    """
    Preview or perform an import of `upload` into `organization`.

    A dry run plans and returns: it opens no transaction and saves nothing.

    A real import is all or nothing. The plan is made inside the transaction
    that writes it, behind a lock on the organization's row, so two imports
    of one file sent at once (a double click on Confirm) run one after the
    other and the second finds the first's customers already on file. A plan
    with an error row is returned unwritten, with `committed` False.
    """
    parsed = read_file(upload)

    if dry_run:
        return _plan(organization=organization, parsed=parsed, dry_run=True, context=context)

    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=organization.pk)
        result = _plan(organization=organization, parsed=parsed, dry_run=False, context=context)
        if not result.has_errors:
            _write(result, organization)
            result.committed = True
    return result
