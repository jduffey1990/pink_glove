"""
Customer import (docs/DECISIONS.md ADR-031).

The contract under test, in the order the classes come:

- who may import (owner and admin, and nobody else);
- a dry run, which is the default, writes nothing;
- a real import is all or nothing;
- which customer a row belongs to, and that a second import of the same file
  adds nothing;
- the tenant boundary;
- access codes go in encrypted and never come back out;
- what a file may look like;
- the template, which is the same column table read the other way;
- the same input gives the same answer.
"""

import csv
import datetime as dt
import io
import logging

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.urls import reverse
from django.utils import timezone
from rest_framework import serializers as drf_serializers
from rest_framework.exceptions import PermissionDenied
from rest_framework.test import APIClient

from base.permissions import ORGANIZATION_INACTIVE
from customers import services
from customers.enums import CustomerStatus, ImportAction
from customers.import_columns import BY_HEADER, COLUMNS, CUSTOMER, LOCATION, MAX_ROWS
from customers.models import Customer, ServiceLocation
from customers.serializers import CustomerSerializer, ImportedLocationSerializer
from customers.views import CustomerViewSet
from organizations.models import Organization
from organizations.services import apply_coverage
from users.enums import Role

IMPORT_URL = reverse("customers:customer-import-customers")
TEMPLATE_URL = reverse("customers:customer-import-template")

# Distinctive on purpose: a plain "1234" would match digits inside a UUID or a
# timestamp and make the "never comes back" assertions pass or fail by luck.
GATE_CODE = "ZEBRA-4417#"
ALARM_CODE = "QUARTZ-9902*"
KEY_LOCATION = "under the blue gnome by the hydrant"
CODES = (GATE_CODE, ALARM_CODE, KEY_LOCATION)


def row(**cells):
    """One file row. A valid customer and address unless a cell says otherwise."""
    return {
        "first_name": "Dana",
        "last_name": "Wu",
        "email": "dana@example.com",
        "address_line1": "1 Elm St",
        "city": "Denver",
        "state": "CO",
        "postal_code": "80202",
        **cells,
    }


def as_csv(rows, headers=None) -> str:
    headers = headers or list(dict.fromkeys(key for item in rows for key in item))
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(headers)
    for item in rows:
        writer.writerow([item.get(header, "") for header in headers])
    return buffer.getvalue()


def send(client, content, dry_run=None, **extra):
    """Upload `content` (text, bytes or a list of row dicts) the way a browser does."""
    if isinstance(content, list):
        content = as_csv(content)
    if isinstance(content, str):
        content = content.encode("utf-8")

    payload = {"file": SimpleUploadedFile("book.csv", content, content_type="text/csv"), **extra}
    if isinstance(dry_run, bool):
        payload["dry_run"] = "true" if dry_run else "false"
    elif dry_run is not None:
        payload["dry_run"] = dry_run  # sent exactly as given
    return client.post(IMPORT_URL, payload, format="multipart")


def stored():
    """Every row there is, soft-deleted included: (customers, locations)."""
    return Customer.all_objects.count(), ServiceLocation.all_objects.count()


def actions(response):
    return [item["action"] for item in response.json()["rows"]]


@pytest.fixture
def client_for(db, make_member):
    def _client(organization, role):
        client = APIClient()
        client.force_login(make_member(organization, role=role, email=f"{role}-import@example.com"))
        return client

    return _client


@pytest.mark.django_db
class TestWhoMayImport:
    @pytest.mark.parametrize("role", [Role.OWNER, Role.ADMIN])
    def test_owner_and_admin_may_import(self, client_for, organization, role):
        client = client_for(organization, role)

        assert send(client, [row()], dry_run=True).status_code == 200
        assert send(client, [row()], dry_run=False).status_code == 200
        assert stored() == (1, 1)

    @pytest.mark.parametrize("role", [Role.DISPATCHER, Role.CLEANER, Role.CUSTOMER])
    @pytest.mark.parametrize("dry_run", [True, False])
    def test_nobody_else_may_even_preview(self, client_for, organization, role, dry_run):
        response = send(client_for(organization, role), [row()], dry_run=dry_run)

        assert response.status_code == 403
        assert stored() == (0, 0)

    def test_anonymous_is_refused(self, api_client, organization):
        assert send(api_client, [row()], dry_run=False).status_code == 403
        assert stored() == (0, 0)

    @pytest.mark.parametrize("role", [Role.OWNER, Role.ADMIN])
    def test_owner_and_admin_may_fetch_the_template(self, client_for, organization, role):
        assert client_for(organization, role).get(TEMPLATE_URL).status_code == 200

    @pytest.mark.parametrize("role", [Role.DISPATCHER, Role.CLEANER, Role.CUSTOMER])
    def test_nobody_else_may_fetch_the_template(self, client_for, organization, role):
        assert client_for(organization, role).get(TEMPLATE_URL).status_code == 403


@pytest.mark.django_db
class TestReadOnlyOrganization:
    """
    A deactivated organization is read-only (ADR-028), whether a person
    switched it off or its subscription lapsed. An import is a write, so it
    is refused where every other write is: `IsOrgMember`, which
    `IsAdminOrHigher` inherits. Nothing in the import knows about billing.
    """

    @pytest.fixture
    def billing_on(self, settings):
        settings.PLATFORM_BILLING_ENABLED = True
        settings.PLATFORM_TRIAL_DAYS = 30
        settings.PLATFORM_GRACE_DAYS = 14

    def lapse(self, organization):
        """Let the trial and its grace run out, then let 4c's own rule switch it off."""
        organization.trial_ends_at = timezone.now() - dt.timedelta(days=40)
        organization.save(update_fields=["trial_ends_at"])
        assert apply_coverage(organization) is True
        organization.refresh_from_db()
        assert organization.is_active is False
        assert organization.deactivated_for_billing_at is not None

    @pytest.mark.parametrize("dry_run", [True, False])
    def test_a_lapsed_organization_is_refused_in_the_servers_words(
        self, authed_client, organization, billing_on, dry_run
    ):
        """
        A dry run too. It is a POST, the rule is by method, and a preview of
        a write that cannot happen would end at a refused Import button.
        """
        self.lapse(organization)

        response = send(authed_client, [row(gate_code=GATE_CODE)], dry_run=dry_run)

        assert response.status_code == 403
        assert response.json() == {"detail": ORGANIZATION_INACTIVE}
        assert stored() == (0, 0)

    @pytest.mark.parametrize("dry_run", [True, False])
    def test_an_organization_switched_off_by_hand_is_refused_the_same_way(
        self, authed_client, organization, dry_run
    ):
        organization.is_active = False
        organization.save(update_fields=["is_active"])

        response = send(authed_client, [row()], dry_run=dry_run)

        assert response.status_code == 403
        assert response.json() == {"detail": ORGANIZATION_INACTIVE}
        assert stored() == (0, 0)

    def test_it_is_refused_exactly_as_creating_a_customer_by_hand_is(
        self, authed_client, organization, billing_on
    ):
        """The same status and the same body as the ordinary create path."""
        self.lapse(organization)

        by_hand = authed_client.post(
            reverse("customers:customer-list"), {"first_name": "Dana"}, format="json"
        )
        by_import = send(authed_client, [row()], dry_run=False)

        assert by_hand.status_code == by_import.status_code == 403
        assert by_hand.json() == by_import.json()

    def test_the_template_can_still_be_read(self, authed_client, organization, billing_on):
        """Read-only, not locked out: a GET is still answered."""
        self.lapse(organization)

        assert authed_client.get(TEMPLATE_URL).status_code == 200

    def test_past_the_trial_but_inside_the_grace_an_import_still_works(
        self, authed_client, organization, billing_on
    ):
        """Not covered, not yet deactivated: every write works, and so does this one."""
        organization.trial_ends_at = timezone.now() - dt.timedelta(days=3)
        organization.save(update_fields=["trial_ends_at"])
        assert apply_coverage(organization) is False

        assert send(authed_client, [row()], dry_run=False).status_code == 200
        assert stored() == (1, 1)

    def test_an_organization_that_is_covered_again_can_import_again(
        self, authed_client, organization, billing_on
    ):
        self.lapse(organization)
        organization.trial_ends_at = timezone.now() + dt.timedelta(days=30)
        organization.save(update_fields=["trial_ends_at"])
        assert apply_coverage(organization) is True

        assert send(authed_client, [row()], dry_run=False).status_code == 200
        assert stored() == (1, 1)

    def test_neither_import_view_opts_out_of_the_read_only_rule(self):
        """`allowed_while_inactive` is for the subscription endpoints alone (ADR-028)."""
        assert not getattr(CustomerViewSet, "allowed_while_inactive", False)
        for name in ("import_customers", "import_template"):
            assert "allowed_while_inactive" not in getattr(CustomerViewSet, name).kwargs

    def test_an_organization_switched_off_while_the_import_waited_is_refused(self, organization):
        """
        The permission check reads the organization the request loaded. A
        real import then waits for the organization's row lock, which 4c's
        checkout holds across calls to Stripe, and the webhook can switch the
        organization off in between. Under the lock the row is read again.
        """
        stale = Organization.objects.get(pk=organization.pk)
        Organization.objects.filter(pk=organization.pk).update(is_active=False)
        assert stale.is_active is True

        upload = SimpleUploadedFile("book.csv", as_csv([row()]).encode())
        with pytest.raises(PermissionDenied) as refused:
            services.import_customers(organization=stale, upload=upload, dry_run=False, context={})

        assert str(refused.value.detail) == ORGANIZATION_INACTIVE
        assert stored() == (0, 0)

    def test_a_superuser_putting_things_right_is_not_refused_by_the_recheck(self, organization):
        """
        `IsOrgMember` lets a superuser write to a deactivated organization.
        The recheck is for one that changed under the request, not that.
        """
        organization.is_active = False
        organization.save(update_fields=["is_active"])

        upload = SimpleUploadedFile("book.csv", as_csv([row()]).encode())
        result = services.import_customers(
            organization=organization, upload=upload, dry_run=False, context={}
        )

        assert result.committed is True
        assert stored() == (1, 1)


@pytest.mark.django_db
class TestDryRun:
    def test_it_is_the_default_and_writes_nothing(self, authed_client):
        """
        The dangerous default. DRF reads a boolean missing from a multipart
        form as False unless the field declares a default, and False here
        means "import for real". This goes red if `default=True` is dropped.
        """
        response = send(authed_client, [row()])

        assert response.status_code == 200
        assert response.json()["dry_run"] is True
        assert response.json()["committed"] is False
        assert stored() == (0, 0)

    def test_an_empty_dry_run_is_still_a_dry_run(self, authed_client):
        response = send(authed_client, [row()], dry_run="")

        assert response.status_code == 200
        assert response.json()["dry_run"] is True
        assert stored() == (0, 0)

    @pytest.mark.parametrize("value", ["banana", "   ", "nope"])
    def test_a_dry_run_that_is_not_a_boolean_is_refused(self, authed_client, value):
        """Refused, rather than read as "not true, so import"."""
        response = send(authed_client, [row()], dry_run=value)

        assert response.status_code == 400
        assert "dry_run" in response.json()
        assert stored() == (0, 0)

    def test_it_says_what_would_happen_and_writes_nothing(self, authed_client, organization):
        existing = Customer.objects.create(
            organization=organization, first_name="Old", email="old@example.com"
        )
        ServiceLocation.objects.create(
            organization=organization,
            customer=existing,
            line1="9 Oak Ave",
            city="Denver",
            state="CO",
            postal_code="80203",
        )
        before = stored()

        response = send(
            authed_client,
            [
                row(),
                row(address_line1="2 Pine Rd"),
                row(
                    first_name="Old",
                    email="old@example.com",
                    address_line1="9 Oak Ave",
                    postal_code="80203",
                ),
                row(email="not-an-email"),
            ],
            dry_run=True,
        )
        body = response.json()

        assert response.status_code == 200
        assert actions(response) == [
            ImportAction.CREATE_CUSTOMER,
            ImportAction.ADD_LOCATION,
            ImportAction.SKIP,
            ImportAction.ERROR,
        ]
        assert [item["row"] for item in body["rows"]] == [2, 3, 4, 5]
        assert body["totals"] == {
            "rows": 4,
            "new_customers": 1,
            "new_locations": 2,
            "skipped": 1,
            "errors": 1,
        }
        assert body["can_commit"] is False
        assert body["rows"][0]["customer"] == "Dana Wu"
        assert body["rows"][0]["address"] == "1 Elm St, Denver, CO, 80202"
        assert body["rows"][3]["errors"] == [
            {"column": "email", "message": "Enter a valid email address."}
        ]
        assert stored() == before

    def test_a_clean_file_can_be_committed_and_says_so(self, authed_client):
        body = send(authed_client, [row()], dry_run=True).json()

        assert body["can_commit"] is True
        assert body["message"] == (
            "Ready to import 1 customer and 1 location. Nothing has been saved yet."
        )

    def test_a_dry_run_with_a_bad_row_is_still_a_200(self, authed_client):
        """A preview that found problems worked. Only a refused write is a 400."""
        response = send(authed_client, [row(email="nope")], dry_run=True)

        assert response.status_code == 200
        assert response.json()["message"] == "Nothing can be imported yet: 1 row needs fixing."


@pytest.mark.django_db
class TestRealImport:
    def test_it_creates_the_customer_and_the_location(self, authed_client, organization):
        response = send(
            authed_client,
            [
                row(
                    phone="303-555-0100",
                    status="active",
                    customer_notes="Prefers mornings.",
                    location_label="Home",
                    square_feet="1800",
                    bathrooms="2.5",
                    has_pets="yes",
                )
            ],
            dry_run=False,
        )
        body = response.json()

        assert response.status_code == 200
        assert body["committed"] is True
        assert body["message"] == "Imported 1 customer and 1 location."

        customer = Customer.objects.get()
        assert customer.organization_id == organization.id
        assert (customer.first_name, customer.last_name) == ("Dana", "Wu")
        assert customer.status == "active"
        assert customer.notes == "Prefers mornings."

        location = ServiceLocation.objects.get()
        assert location.organization_id == organization.id
        assert location.customer_id == customer.id
        assert location.label == "Home"
        assert location.square_feet == 1800
        assert str(location.bathrooms) == "2.5"
        assert location.has_pets is True

    def test_a_blank_cell_takes_the_fields_own_default(self, authed_client):
        """Blank is "not given", not an empty string pushed at a choice field."""
        send(authed_client, [row(preferred_contact_method="", has_pets="")], dry_run=False)

        field = Customer._meta.get_field("preferred_contact_method")
        assert Customer.objects.get().preferred_contact_method == field.default
        assert ServiceLocation.objects.get().has_pets is False

    def test_one_bad_row_means_nothing_is_written(self, authed_client):
        response = send(
            authed_client,
            [row(), row(email="b@example.com", city=""), row(email="c@example.com")],
            dry_run=False,
        )
        body = response.json()

        assert response.status_code == 400
        assert body["committed"] is False
        assert body["message"] == "Nothing was imported: 1 row needs fixing."
        assert actions(response) == [
            ImportAction.CREATE_CUSTOMER,
            ImportAction.ERROR,
            ImportAction.CREATE_CUSTOMER,
        ]
        assert body["rows"][1]["row"] == 3
        assert body["rows"][1]["errors"] == [
            {"column": "city", "message": "This field is required."}
        ]
        assert stored() == (0, 0)

    def test_every_problem_with_a_row_is_reported_at_once(self, authed_client):
        body = send(
            authed_client,
            [row(first_name="", last_name="", email="nope", status="vip", city="")],
            dry_run=False,
        ).json()

        assert body["rows"][0]["errors"] == [
            {"column": "email", "message": "Enter a valid email address."},
            {"column": "status", "message": '"vip" is not a valid choice.'},
            {"column": "city", "message": "This field is required."},
        ]

    def test_a_row_is_held_to_the_create_paths_own_rules(self, authed_client):
        """
        Not a copy of them: the message for a nameless customer is whatever
        `CustomerSerializer` says, so changing the rule changes the import.
        """
        serializer = CustomerSerializer(data={"email": "x@example.com"})
        assert not serializer.is_valid()
        expected = str(serializer.errors["non_field_errors"][0])

        body = send(authed_client, [row(first_name="", last_name="")], dry_run=False).json()

        assert body["rows"][0]["errors"] == [{"column": "", "message": expected}]

    @pytest.mark.parametrize("dry_run", [True, False])
    def test_an_error_on_a_field_no_column_maps_to_still_stops_the_row(
        self, authed_client, monkeypatch, dry_run
    ):
        """
        A rule added to the create path later may object to a field the file
        has no column for. The row is still an error and still says why:
        reading only the mapped columns' errors would call it importable.
        """

        def refuse(self, attrs):
            raise drf_serializers.ValidationError({"user": ["That login is taken."]})

        monkeypatch.setattr(CustomerSerializer, "validate", refuse)

        response = send(authed_client, [row()], dry_run=dry_run)

        assert response.status_code == (200 if dry_run else 400)
        assert actions(response) == [ImportAction.ERROR]
        assert response.json()["can_commit"] is False
        assert response.json()["rows"][0]["errors"] == [
            {"column": "", "message": "That login is taken."}
        ]
        assert stored() == (0, 0)

    def test_a_failure_while_writing_rolls_the_whole_import_back(self, authed_client, monkeypatch):
        """
        The plan was clean, so this is the database or the code failing half
        way through. The rows already saved must not survive.
        """
        real_save = ImportedLocationSerializer.save
        calls = []

        def save_once_then_fail(self, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("the second location could not be saved")
            return real_save(self, **kwargs)

        monkeypatch.setattr(ImportedLocationSerializer, "save", save_once_then_fail)

        with pytest.raises(RuntimeError):
            send(authed_client, [row(), row(email="b@example.com")], dry_run=False)

        assert len(calls) == 2  # the first customer and location really were saved
        assert stored() == (0, 0)

    def test_a_json_body_is_refused(self, authed_client):
        response = authed_client.post(IMPORT_URL, {"dry_run": False}, format="json")

        assert response.status_code == 415

    def test_a_request_with_no_file_is_refused(self, authed_client):
        response = authed_client.post(IMPORT_URL, {"dry_run": "false"}, format="multipart")

        assert response.status_code == 400
        assert "file" in response.json()


@pytest.mark.django_db
class TestImportedStatus:
    """
    The one default an import has that the create path does not (ADR-031): a
    blank or missing status is `active`, because a customer book is customers.
    The API's own default stays `lead`.
    """

    def test_a_blank_status_cell_is_an_active_customer(self, authed_client):
        response = send(authed_client, [row(status="")], dry_run=False)

        assert response.status_code == 200
        assert Customer.objects.get().status == CustomerStatus.ACTIVE

    def test_a_file_with_no_status_column_is_active_customers_too(self, authed_client):
        book = [row()]
        assert "status" not in as_csv(book).splitlines()[0]

        send(authed_client, book, dry_run=False)

        assert Customer.objects.get().status == CustomerStatus.ACTIVE

    @pytest.mark.parametrize("status", ["lead", "active", "inactive", "archived"])
    def test_a_status_that_is_given_is_kept_as_written(self, authed_client, status):
        send(authed_client, [row(status=status)], dry_run=False)

        assert Customer.objects.get().status == status

    @pytest.mark.parametrize("dry_run", [True, False])
    def test_a_status_that_is_not_one_is_still_a_row_error(self, authed_client, dry_run):
        response = send(authed_client, [row(status="vip")], dry_run=dry_run)

        assert actions(response) == [ImportAction.ERROR]
        assert response.json()["rows"][0]["errors"] == [
            {"column": "status", "message": '"vip" is not a valid choice.'}
        ]
        assert stored() == (0, 0)

    def test_the_preview_and_the_import_say_the_same_thing(self, authed_client):
        book = [row(status=""), row(email="b@example.com", status="lead")]

        preview = send(authed_client, book, dry_run=True)
        assert stored() == (0, 0)
        real = send(authed_client, book, dry_run=False)

        assert preview.status_code == real.status_code == 200
        assert preview.json()["rows"] == real.json()["rows"]
        assert preview.json()["totals"] == real.json()["totals"]
        assert list(
            Customer.objects.order_by("created_at", "id").values_list("email", "status")
        ) == [("dana@example.com", "active"), ("b@example.com", "lead")]

    def test_importing_a_file_of_blank_statuses_twice_adds_and_changes_nothing(self, authed_client):
        book = [row(status=""), row(email="b@example.com", status="")]
        send(authed_client, book, dry_run=False)
        before = list(Customer.objects.order_by("id").values_list("id", "status", "updated_at"))

        second = send(authed_client, book, dry_run=False)

        assert actions(second) == [ImportAction.SKIP, ImportAction.SKIP]
        assert stored() == (2, 2)
        assert (
            list(Customer.objects.order_by("id").values_list("id", "status", "updated_at"))
            == before
        )

    def test_a_customer_already_on_file_is_not_made_active(self, authed_client, organization):
        """The default is for customers the import creates. It never edits one."""
        existing = Customer.objects.create(
            organization=organization,
            first_name="Dana",
            email="dana@example.com",
            status=CustomerStatus.LEAD,
        )

        response = send(authed_client, [row(status="")], dry_run=False)

        assert actions(response) == [ImportAction.ADD_LOCATION]
        existing.refresh_from_db()
        assert existing.status == CustomerStatus.LEAD

    def test_the_create_path_still_defaults_to_lead(self, authed_client):
        """Imports only: the API's own default is untouched."""
        response = authed_client.post(
            reverse("customers:customer-list"), {"first_name": "Dana"}, format="json"
        )

        assert response.status_code == 201
        assert response.json()["status"] == CustomerStatus.LEAD
        assert Customer._meta.get_field("status").default == CustomerStatus.LEAD

    def test_the_template_says_what_a_blank_status_means(self, authed_client):
        hints = {
            column["header"]: column["hint"]
            for column in authed_client.get(TEMPLATE_URL).json()["columns"]
        }

        assert hints["status"] == (
            "One of: lead, active, inactive, archived. Blank is imported as active."
        )

    def test_status_is_the_only_import_default_and_it_is_a_valid_one(self):
        """A second one is a decision, and a default must pass its own field."""
        defaults = {column.header: column.when_blank for column in COLUMNS if column.when_blank}

        assert defaults == {"status": CustomerStatus.ACTIVE.value}
        assert defaults["status"] in CustomerSerializer().fields["status"].choices


@pytest.mark.django_db
class TestMatching:
    """Which customer a row belongs to (ADR-031)."""

    def test_rows_with_one_email_are_one_customer_with_several_locations(self, authed_client):
        response = send(
            authed_client,
            [
                row(location_label="Home"),
                row(email="DANA@Example.com ", address_line1="2 Pine Rd", location_label="Rental"),
            ],
            dry_run=False,
        )

        assert actions(response) == [ImportAction.CREATE_CUSTOMER, ImportAction.ADD_LOCATION]
        assert response.json()["rows"][1]["detail"] == "Another location for the customer on row 2."
        customer = Customer.objects.get()
        assert sorted(customer.locations.values_list("label", flat=True)) == ["Home", "Rental"]

    def test_the_first_row_for_a_customer_supplies_their_details(self, authed_client):
        send(
            authed_client,
            [row(phone="303-555-0100"), row(phone="720-555-0199", address_line1="2 Pine Rd")],
            dry_run=False,
        )

        assert Customer.objects.get().phone == "303-555-0100"

    def test_without_an_email_the_name_and_phone_are_the_key(self, authed_client):
        response = send(
            authed_client,
            [
                row(email="", phone="(303) 555-0100"),
                row(email="", phone="303-555-0100", first_name="dana ", address_line1="2 Pine Rd"),
            ],
            dry_run=False,
        )

        assert actions(response) == [ImportAction.CREATE_CUSTOMER, ImportAction.ADD_LOCATION]
        assert stored() == (1, 2)

    def test_without_an_email_a_different_phone_is_a_different_customer(self, authed_client):
        send(
            authed_client,
            [
                row(email="", phone="303-555-0100"),
                row(email="", phone="720-555-0199", address_line1="2 Pine Rd"),
            ],
            dry_run=False,
        )

        assert stored() == (2, 2)

    def test_a_row_with_no_email_never_matches_a_customer_who_has_one(
        self, authed_client, organization
    ):
        """One key per customer, so the answer does not depend on row order."""
        Customer.objects.create(
            organization=organization, first_name="Dana", last_name="Wu", email="dana@example.com"
        )

        response = send(authed_client, [row(email="")], dry_run=False)

        assert actions(response) == [ImportAction.CREATE_CUSTOMER]
        assert Customer.objects.count() == 2

    def test_importing_the_same_file_twice_adds_nothing(self, authed_client):
        book = [
            row(gate_code=GATE_CODE),
            row(address_line1="2 Pine Rd"),
            row(email="", first_name="Sam", last_name="Ortiz", phone="303-555-0111"),
            row(email="", first_name="", last_name="", company_name="Acme LLC"),
        ]
        first = send(authed_client, book, dry_run=False)
        after_first = stored()

        second = send(authed_client, book, dry_run=False)

        assert first.status_code == second.status_code == 200
        assert after_first == (3, 4)
        assert stored() == after_first
        assert actions(second) == [ImportAction.SKIP] * 4
        assert second.json()["totals"]["skipped"] == 4
        assert second.json()["can_commit"] is False
        assert second.json()["message"] == "Nothing was imported: every row is already on file."
        assert {item["detail"] for item in second.json()["rows"]} == {
            "Already on file: this customer has this address."
        }

    def test_the_same_row_twice_in_one_file_is_one_location(self, authed_client):
        response = send(authed_client, [row(), row(address_line1="1  ELM st")], dry_run=False)

        assert actions(response) == [ImportAction.CREATE_CUSTOMER, ImportAction.SKIP]
        assert response.json()["rows"][1]["detail"] == "Same customer and address as row 2."
        assert stored() == (1, 1)

    def test_an_existing_customer_gains_a_new_location_and_is_not_edited(
        self, authed_client, organization
    ):
        existing = Customer.objects.create(
            organization=organization,
            first_name="Dana",
            last_name="Wu",
            email="Dana@Example.com",
            phone="303-555-0100",
            notes="Typed in by hand.",
        )

        response = send(
            authed_client,
            [row(first_name="Danielle", phone="720-555-0199", customer_notes="From the file.")],
            dry_run=False,
        )

        assert actions(response) == [ImportAction.ADD_LOCATION]
        assert response.json()["rows"][0]["detail"] == (
            "New location for Dana Wu, who is already a customer."
        )
        existing.refresh_from_db()
        assert existing.first_name == "Dana"
        assert existing.phone == "303-555-0100"
        assert existing.notes == "Typed in by hand."
        assert existing.locations.count() == 1
        assert Customer.objects.count() == 1

    def test_an_existing_location_is_skipped_and_its_codes_are_left_alone(
        self, authed_client, organization
    ):
        existing = Customer.objects.create(
            organization=organization, first_name="Dana", email="dana@example.com"
        )
        location = ServiceLocation.objects.create(
            organization=organization,
            customer=existing,
            line1="1 Elm St",
            city="Denver",
            state="CO",
            postal_code="80202",
            gate_code="the-code-on-file",
        )

        response = send(authed_client, [row(gate_code=GATE_CODE)], dry_run=False)

        assert actions(response) == [ImportAction.SKIP]
        location.refresh_from_db()
        assert location.gate_code == "the-code-on-file"
        assert stored() == (1, 1)

    def test_a_deleted_customer_is_not_on_file(self, authed_client, organization):
        gone = Customer.objects.create(
            organization=organization, first_name="Dana", email="dana@example.com"
        )
        gone.delete()

        response = send(authed_client, [row()], dry_run=False)

        assert actions(response) == [ImportAction.CREATE_CUSTOMER]
        assert Customer.objects.count() == 1

    def test_a_row_that_matches_two_customers_is_an_error_not_a_guess(
        self, authed_client, organization
    ):
        for first_name in ("Dana", "Dan"):
            Customer.objects.create(
                organization=organization, first_name=first_name, email="dana@example.com"
            )
        before = stored()

        response = send(authed_client, [row()], dry_run=False)

        assert response.status_code == 400
        assert actions(response) == [ImportAction.ERROR]
        error = response.json()["rows"][0]["errors"][0]
        assert error["column"] == "email"
        assert error["message"].startswith("2 customers here already match this row")
        assert stored() == before


@pytest.mark.django_db
class TestTenantBoundary:
    def test_an_import_creates_nothing_in_another_organization(
        self, authed_client, organization, other_organization
    ):
        send(authed_client, [row(), row(email="b@example.com")], dry_run=False)

        assert Customer.objects.filter(organization=organization).count() == 2
        assert ServiceLocation.objects.filter(organization=organization).count() == 2
        assert not Customer.all_objects.filter(organization=other_organization).exists()
        assert not ServiceLocation.all_objects.filter(organization=other_organization).exists()

    def test_a_forged_organization_in_the_form_is_ignored(
        self, authed_client, organization, other_organization
    ):
        send(authed_client, [row()], dry_run=False, organization=str(other_organization.id))

        assert Customer.objects.get().organization_id == organization.id
        assert ServiceLocation.objects.get().organization_id == organization.id

    def test_an_organization_column_in_the_file_is_ignored(
        self, authed_client, organization, other_organization
    ):
        response = send(
            authed_client, [row(organization=str(other_organization.id))], dry_run=False
        )

        assert response.json()["ignored_columns"] == ["organization"]
        assert Customer.objects.get().organization_id == organization.id

    def test_matching_never_sees_another_organizations_customer(
        self, authed_client, organization, other_organization
    ):
        """
        The rival has this very customer at this very address. Seen from here
        the row is brand new, and their record is not touched or attached to.
        """
        theirs = Customer.objects.create(
            organization=other_organization, first_name="Dana", email="dana@example.com"
        )
        ServiceLocation.objects.create(
            organization=other_organization,
            customer=theirs,
            line1="1 Elm St",
            city="Denver",
            state="CO",
            postal_code="80202",
        )

        response = send(authed_client, [row()], dry_run=False)

        assert actions(response) == [ImportAction.CREATE_CUSTOMER]
        mine = Customer.objects.get(organization=organization)
        assert mine.id != theirs.id
        assert mine.locations.count() == 1
        assert theirs.locations.count() == 1
        assert Customer.objects.filter(organization=other_organization).count() == 1

    def test_the_rival_importing_the_same_file_gets_their_own_copy(
        self, authed_client, organization, other_organization, rival_owner
    ):
        send(authed_client, [row()], dry_run=False)
        rival = APIClient()
        rival.force_login(rival_owner)

        response = send(rival, [row()], dry_run=False)

        assert actions(response) == [ImportAction.CREATE_CUSTOMER]
        assert Customer.objects.filter(organization=organization).count() == 1
        assert Customer.objects.filter(organization=other_organization).count() == 1


@pytest.mark.django_db
class TestAccessCodes:
    """ADR-014, ADR-016: codes go in encrypted and are never sent back or logged."""

    BOOK = [row(gate_code=GATE_CODE, alarm_code=ALARM_CODE, key_location=KEY_LOCATION)]

    @pytest.fixture(autouse=True)
    def _capture_everything(self, caplog):
        caplog.set_level(logging.DEBUG)

    @pytest.mark.parametrize("dry_run", [True, False])
    def test_a_code_is_in_neither_the_response_nor_the_logs(self, authed_client, caplog, dry_run):
        response = send(authed_client, self.BOOK, dry_run=dry_run)

        assert response.status_code == 200
        text = response.content.decode()
        for code in CODES:
            assert code not in text
            assert code not in caplog.text
        # Guard the guard: the log was captured, and it is the counts-only line.
        assert "Customer import:" in caplog.text
        assert "dana@example.com" not in caplog.text
        assert "1 Elm St" not in caplog.text

    def test_a_code_is_stored_and_read_back_by_the_model(self, authed_client):
        send(authed_client, self.BOOK, dry_run=False)

        location = ServiceLocation.objects.get()
        assert location.gate_code == GATE_CODE
        assert location.alarm_code == ALARM_CODE
        assert location.key_location == KEY_LOCATION

    def test_a_code_is_ciphertext_in_the_database(self, authed_client):
        send(authed_client, self.BOOK, dry_run=False)

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT gate_code, alarm_code, key_location FROM customers_servicelocation"
            )
            raw = cursor.fetchone()

        assert all(raw)
        for code, column in zip(CODES, raw, strict=True):
            assert code not in column

    def test_a_code_comes_back_only_through_the_audited_reveal(self, authed_client):
        send(authed_client, self.BOOK, dry_run=False)
        location = ServiceLocation.objects.get()

        detail = authed_client.get(reverse("customers:location-detail", args=[location.id]))
        assert GATE_CODE not in detail.content.decode()
        assert detail.json()["has_access_codes"] is True

        revealed = authed_client.post(
            reverse("customers:location-reveal-access", args=[location.id]),
            {"acknowledged": True},
            format="json",
        )
        assert revealed.status_code == 200
        assert revealed.json()["gate_code"] == GATE_CODE
        assert revealed.json()["alarm_code"] == ALARM_CODE
        assert revealed.json()["key_location"] == KEY_LOCATION

    @pytest.mark.parametrize("dry_run", [True, False])
    def test_an_error_row_does_not_echo_its_codes(self, authed_client, caplog, dry_run):
        response = send(
            authed_client,
            [
                row(
                    email="not-an-email",
                    city="",
                    gate_code=GATE_CODE,
                    alarm_code=ALARM_CODE,
                    key_location=KEY_LOCATION,
                )
            ],
            dry_run=dry_run,
        )

        assert response.status_code == (200 if dry_run else 400)
        assert actions(response) == [ImportAction.ERROR]
        for code in CODES:
            assert code not in response.content.decode()
            assert code not in caplog.text
        assert stored() == (0, 0)

    def test_a_code_the_field_itself_refuses_is_not_echoed(self, authed_client, caplog):
        """The error is about the code cell, and still does not quote it."""
        bad_code = f"{GATE_CODE}\x00"

        response = send(authed_client, [row(gate_code=bad_code)], dry_run=False)

        assert response.status_code == 400
        assert [item["column"] for item in response.json()["rows"][0]["errors"]] == ["gate_code"]
        assert GATE_CODE not in response.content.decode()
        assert GATE_CODE not in caplog.text

    def test_a_refused_file_does_not_echo_its_content(self, authed_client, caplog):
        content = as_csv([row(gate_code=GATE_CODE)]).replace("city", "town")

        response = send(authed_client, content, dry_run=False)

        assert response.status_code == 400
        assert GATE_CODE not in response.content.decode()
        assert GATE_CODE not in caplog.text

    def test_an_outcome_has_nowhere_to_put_a_code(self, authed_client):
        """The response's shape, pinned: a new field has to be a decision."""
        item = send(authed_client, self.BOOK).json()["rows"][0]

        assert set(item) == {"row", "action", "customer", "address", "detail", "errors"}


@pytest.mark.django_db
class TestTheFile:
    def test_a_byte_order_mark_is_not_part_of_the_first_header(self, authed_client):
        content = b"\xef\xbb\xbf" + as_csv([row()]).encode("utf-8")

        response = send(authed_client, content, dry_run=False)

        assert response.status_code == 200
        assert response.json()["ignored_columns"] == []
        assert Customer.objects.get().first_name == "Dana"

    def test_text_outside_ascii_survives(self, authed_client):
        send(authed_client, [row(first_name="Zoë", last_name="Núñez")], dry_run=False)

        assert Customer.objects.get().display_name == "Zoë Núñez"

    @pytest.mark.parametrize("encoding", ["latin-1", "utf-16"])
    def test_another_encoding_is_refused_and_says_what_to_do(self, authed_client, encoding):
        content = as_csv([row(first_name="Zoë")]).encode(encoding)

        response = send(authed_client, content, dry_run=False)

        assert response.status_code == 400
        assert "UTF-8" in response.json()["file"][0]
        assert stored() == (0, 0)

    def test_headers_are_matched_whatever_their_case_and_spacing(self, authed_client):
        content = (
            " First Name ,LAST_NAME,Address-Line1,  City,state ,Postal Code\r\n"
            "Dana,Wu,1 Elm St,Denver,CO,80202\r\n"
        )

        response = send(authed_client, content, dry_run=False)

        assert response.status_code == 200
        assert response.json()["ignored_columns"] == []
        assert ServiceLocation.objects.get().one_line_address == "1 Elm St, Denver, CO, 80202"

    def test_a_missing_required_column_is_named(self, authed_client):
        content = "first_name,address_line1,state\r\nDana,1 Elm St,CO\r\n"

        response = send(authed_client, content, dry_run=False)

        assert response.status_code == 400
        assert response.json()["file"] == [
            "This file is missing required columns: city, postal_code. "
            "The template has the full list."
        ]

    def test_unknown_columns_are_reported_not_silently_dropped(self, authed_client):
        response = send(
            authed_client, [row(**{"Balance Due": "12.50", "favourite colour": "teal"})]
        )

        assert response.status_code == 200
        assert response.json()["ignored_columns"] == ["Balance Due", "favourite colour"]
        assert actions(response) == [ImportAction.CREATE_CUSTOMER]

    def test_a_column_with_no_header_is_reported_by_its_position(self, authed_client):
        content = (
            "first_name,,address_line1,city,state,postal_code\r\n"
            "Dana,stray,1 Elm St,Denver,CO,80202\r\n"
        )

        response = send(authed_client, content)

        assert response.json()["ignored_columns"] == ["(column 2, no header)"]
        assert actions(response) == [ImportAction.CREATE_CUSTOMER]

    def test_a_file_the_csv_reader_cannot_take_is_refused(self, authed_client):
        """One cell past the reader's own field limit, well inside the size cap."""
        content = as_csv([row(customer_notes="x" * 200_000)])

        response = send(authed_client, content, dry_run=False)

        assert response.status_code == 400
        assert response.json()["file"] == ["This file could not be read as CSV."]
        assert stored() == (0, 0)

    def test_a_column_given_twice_is_refused(self, authed_client):
        content = (
            "first_name,First Name,address_line1,city,state,postal_code\r\n"
            "Dana,D,1 Elm St,Denver,CO,80202\r\n"
        )

        response = send(authed_client, content, dry_run=False)

        assert response.status_code == 400
        assert response.json()["file"] == ["These columns appear more than once: first_name."]

    def test_blank_rows_are_ignored_and_do_not_shift_the_numbering(self, authed_client):
        header = "first_name,email,address_line1,city,state,postal_code"
        content = "\r\n".join(
            [
                header,
                "Dana,dana@example.com,1 Elm St,Denver,CO,80202",
                ",,,,,",
                "",
                "Sam,nope,2 Pine Rd,Denver,CO,80203",
                ",,,,,",
                "   ",
                "",
            ]
        )

        response = send(authed_client, content)

        assert [(item["row"], item["action"]) for item in response.json()["rows"]] == [
            (2, ImportAction.CREATE_CUSTOMER),
            (5, ImportAction.ERROR),
        ]

    def test_a_short_row_reads_its_missing_cells_as_blank(self, authed_client):
        content = (
            "first_name,address_line1,city,state,postal_code,parking_notes,pet_notes\r\n"
            "Dana,1 Elm St,Denver,CO,80202\r\n"
        )

        assert actions(send(authed_client, content)) == [ImportAction.CREATE_CUSTOMER]

    def test_a_row_with_too_many_cells_is_an_error(self, authed_client):
        """An unquoted comma in an address shifts every cell after it."""
        content = (
            "first_name,address_line1,city,state,postal_code\r\n"
            "Dana,1 Elm St, Apt 4,Denver,CO,80202\r\n"
        )

        body = send(authed_client, content).json()

        assert body["rows"][0]["action"] == ImportAction.ERROR
        assert body["rows"][0]["errors"][-1]["column"] == ""
        assert "more cells than the header" in body["rows"][0]["errors"][-1]["message"]

    def test_trailing_commas_are_not_extra_cells(self, authed_client):
        content = (
            "first_name,address_line1,city,state,postal_code,,\r\n"
            "Dana,1 Elm St,Denver,CO,80202,,\r\n"
        )

        response = send(authed_client, content)

        assert response.json()["ignored_columns"] == []
        assert actions(response) == [ImportAction.CREATE_CUSTOMER]

    def test_a_quoted_cell_may_hold_commas_and_line_breaks(self, authed_client):
        send(
            authed_client,
            [row(access_notes="Ring twice, then wait.\nSide door.")],
            dry_run=False,
        )

        assert ServiceLocation.objects.get().access_notes == "Ring twice, then wait.\nSide door."

    def test_too_many_rows_is_refused(self, authed_client, monkeypatch):
        monkeypatch.setattr(services, "MAX_ROWS", 3)
        book = [row(email=f"c{number}@example.com") for number in range(4)]

        response = send(authed_client, book, dry_run=False)

        assert response.status_code == 400
        assert response.json()["file"] == [
            "This file has 4 rows; the limit is 3. Split it and import each part."
        ]
        assert stored() == (0, 0)

    def test_the_row_limit_itself_is_allowed(self, authed_client, monkeypatch):
        monkeypatch.setattr(services, "MAX_ROWS", 3)
        book = [row(email=f"c{number}@example.com") for number in range(3)]

        assert send(authed_client, book, dry_run=False).status_code == 200
        assert stored() == (3, 3)

    def test_the_row_limit_is_about_two_thousand(self):
        assert MAX_ROWS == 2000

    def test_a_file_over_the_size_limit_is_refused(self, authed_client):
        content = as_csv([row(customer_notes="x" * (1024 * 1024))])

        response = send(authed_client, content, dry_run=False)

        assert response.status_code == 400
        assert "the limit is 1 MB" in response.json()["file"][0]
        assert stored() == (0, 0)

    @pytest.mark.parametrize("content", ["\r\n\r\n", " , ,\r\n"])
    def test_a_file_with_nothing_in_it_is_refused(self, authed_client, content):
        response = send(authed_client, content, dry_run=False)

        assert response.status_code == 400
        assert response.json()["file"] == ["This file is empty."]

    def test_a_header_with_nothing_under_it_is_refused(self, authed_client):
        content = "first_name,address_line1,city,state,postal_code\r\n\r\n,,,,\r\n"

        response = send(authed_client, content, dry_run=False)

        assert response.status_code == 400
        assert response.json()["file"] == ["This file has a header row and nothing under it."]


@pytest.mark.django_db
class TestTemplate:
    def test_it_lists_the_columns_in_the_tables_order(self, authed_client):
        body = authed_client.get(TEMPLATE_URL).json()

        assert [column["header"] for column in body["columns"]] == [
            column.header for column in COLUMNS
        ]
        assert body["filename"].endswith(".csv")
        assert body["max_rows"] == MAX_ROWS
        assert body["max_megabytes"] == 1

    def test_required_is_read_off_the_serializers(self, authed_client):
        body = authed_client.get(TEMPLATE_URL).json()

        required = [column["header"] for column in body["columns"] if column["required"]]
        assert required == ["address_line1", "city", "state", "postal_code"]
        assert required == services.required_headers()

    def test_a_choice_column_lists_its_choices(self, authed_client):
        hints = {
            column["header"]: column["hint"]
            for column in authed_client.get(TEMPLATE_URL).json()["columns"]
        }

        # `sms` is refused until SMS ships (ADR-035), so the hint does not offer it.
        assert hints["preferred_contact_method"] == "One of: email, phone."
        assert hints["has_pets"] == "yes or no."
        assert hints["gate_code"] == "Stored encrypted. It is not shown again after the import."

    def test_the_csv_is_the_header_and_one_example(self, authed_client):
        records = list(csv.reader(io.StringIO(authed_client.get(TEMPLATE_URL).json()["csv"])))

        assert len(records) == 2
        assert records[0] == [column.header for column in COLUMNS]

    def test_the_template_imports_as_it_stands(self, authed_client):
        """The example row is held to the importer's own rules, so it cannot rot."""
        template = authed_client.get(TEMPLATE_URL).json()["csv"]

        response = send(authed_client, template, dry_run=False)
        body = response.json()

        assert response.status_code == 200
        assert body["ignored_columns"] == []
        assert body["totals"] == {
            "rows": 1,
            "new_customers": 1,
            "new_locations": 1,
            "skipped": 0,
            "errors": 0,
        }

    def test_the_example_row_carries_no_access_code(self):
        """A template gets emailed around. It must not teach people to put codes in one."""
        fields = ImportedLocationSerializer().fields
        write_only = [
            column
            for column in COLUMNS
            if column.target == LOCATION and fields[column.field].write_only
        ]

        assert [column.header for column in write_only] == [
            "gate_code",
            "alarm_code",
            "key_location",
        ]
        assert all(column.example == "" for column in write_only)

    def test_every_column_names_a_writable_field(self):
        """A typo in the table fails here, not as a 500 on somebody's upload."""
        fields = {
            CUSTOMER: CustomerSerializer().fields,
            LOCATION: ImportedLocationSerializer().fields,
        }

        for column in COLUMNS:
            assert column.field in fields[column.target], column.header
            assert not fields[column.target][column.field].read_only, column.header

        assert len(BY_HEADER) == len(COLUMNS), "two columns normalize to one header"
        pairs = [(column.target, column.field) for column in COLUMNS]
        assert len(set(pairs)) == len(pairs), "one field is mapped twice"

    def test_the_customer_is_never_taken_from_the_file(self):
        assert ImportedLocationSerializer().fields["customer"].read_only
        assert "user" not in {column.field for column in COLUMNS if column.target == CUSTOMER}
        assert "organization" not in {column.field for column in COLUMNS}


@pytest.mark.django_db
class TestDeterminism:
    """One green run proves it completes. Two identical runs prove it is stable."""

    BOOK = [
        row(email="", first_name="Sam", last_name="Ortiz", phone="303-555-0111"),
        row(),
        row(address_line1="2 Pine Rd"),
        row(email="nope"),
        row(email="", first_name="Sam", last_name="Ortiz", phone="303-555-0111"),
        row(email="", first_name="", last_name="", company_name="Acme LLC", city=""),
        row(email="zed@example.com", first_name="Zed", last_name="Abel"),
    ]

    def test_two_dry_runs_of_one_file_are_identical(self, authed_client, organization):
        for first_name in ("Dana", "Dan"):
            Customer.objects.create(
                organization=organization, first_name=first_name, email="zed@example.com"
            )

        first = send(authed_client, self.BOOK, dry_run=True)
        second = send(authed_client, self.BOOK, dry_run=True)

        assert first.status_code == second.status_code == 200
        assert first.content == second.content
        assert [item["row"] for item in first.json()["rows"]] == [2, 3, 4, 5, 6, 7, 8]

    def test_two_imports_of_one_file_store_the_same_things_in_the_same_order(
        self, authed_client, organization, other_organization, rival_owner
    ):
        book = [item for item in self.BOOK if item.get("email") != "nope" and item["city"]]
        rival = APIClient()
        rival.force_login(rival_owner)

        first = send(authed_client, book, dry_run=False)
        second = send(rival, book, dry_run=False)

        assert first.status_code == second.status_code == 200
        assert first.content == second.content

        def book_of(tenant):
            return [
                (
                    customer.display_name,
                    customer.email,
                    [
                        location.one_line_address
                        for location in customer.locations.order_by("created_at", "id")
                    ],
                )
                for customer in Customer.objects.filter(organization=tenant).order_by(
                    "created_at", "id"
                )
            ]

        assert book_of(organization) == book_of(other_organization)
        assert [name for name, _, _ in book_of(organization)] == [
            "Sam Ortiz",
            "Dana Wu",
            "Zed Abel",
        ]
        assert book_of(organization)[1][2] == [
            "1 Elm St, Denver, CO, 80202",
            "2 Pine Rd, Denver, CO, 80202",
        ]
