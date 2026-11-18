"""
No customer may be given the "Text message" preference until SMS exists
(ADR-035). Refused on every write path; a row that already holds it still
reads, and the other preferences are untouched.
"""

import pytest
from django.urls import reverse

from customers.enums import ContactMethod
from customers.models import Customer
from customers.serializers import SMS_UNAVAILABLE
from customers.tests.test_import import row, send, stored

LIST_URL = reverse("customers:customer-list")


def detail(customer):
    return reverse("customers:customer-detail", args=[customer.id])


@pytest.fixture
def customer(organization):
    return Customer.objects.create(organization=organization, first_name="Dana", last_name="Wu")


@pytest.mark.django_db
class TestCreate:
    def test_sms_is_refused_with_its_own_sentence(self, authed_client):
        response = authed_client.post(
            LIST_URL,
            {"first_name": "Dana", "preferred_contact_method": ContactMethod.SMS},
            format="json",
        )

        assert response.status_code == 400
        assert response.json() == {"preferred_contact_method": [SMS_UNAVAILABLE]}
        assert SMS_UNAVAILABLE == "Text messages aren't available yet."
        assert not Customer.objects.exists()

    @pytest.mark.parametrize("method", [ContactMethod.EMAIL, ContactMethod.PHONE])
    def test_email_and_phone_still_are_accepted(self, authed_client, method):
        response = authed_client.post(
            LIST_URL, {"first_name": "Dana", "preferred_contact_method": method}, format="json"
        )

        assert response.status_code == 201
        assert Customer.objects.get().preferred_contact_method == method


@pytest.mark.django_db
class TestUpdate:
    def test_a_patch_to_sms_is_refused(self, authed_client, customer):
        response = authed_client.patch(
            detail(customer), {"preferred_contact_method": ContactMethod.SMS}, format="json"
        )

        assert response.status_code == 400
        assert response.json() == {"preferred_contact_method": [SMS_UNAVAILABLE]}
        customer.refresh_from_db()
        assert customer.preferred_contact_method == ContactMethod.EMAIL

    def test_a_put_with_sms_is_refused(self, authed_client, customer):
        response = authed_client.put(
            detail(customer),
            {"first_name": "Dana", "preferred_contact_method": ContactMethod.SMS},
            format="json",
        )

        assert response.status_code == 400
        assert response.json() == {"preferred_contact_method": [SMS_UNAVAILABLE]}

    def test_a_patch_to_phone_is_accepted(self, authed_client, customer):
        response = authed_client.patch(
            detail(customer), {"preferred_contact_method": ContactMethod.PHONE}, format="json"
        )

        assert response.status_code == 200


@pytest.mark.django_db
class TestExistingSmsRows:
    """No data migration: a customer who already has SMS keeps reading and editing."""

    @pytest.fixture
    def texter(self, customer):
        customer.preferred_contact_method = ContactMethod.SMS
        customer.save()
        return customer

    def test_it_still_reads(self, authed_client, texter):
        response = authed_client.get(detail(texter))

        assert response.status_code == 200
        assert response.json()["preferred_contact_method"] == ContactMethod.SMS

    def test_another_field_can_still_be_changed(self, authed_client, texter):
        response = authed_client.patch(detail(texter), {"notes": "Back door."}, format="json")

        assert response.status_code == 200
        texter.refresh_from_db()
        assert texter.notes == "Back door."
        assert texter.preferred_contact_method == ContactMethod.SMS

    def test_it_can_be_moved_off_sms(self, authed_client, texter):
        response = authed_client.patch(
            detail(texter), {"preferred_contact_method": ContactMethod.EMAIL}, format="json"
        )

        assert response.status_code == 200


@pytest.mark.django_db
class TestImport:
    def test_an_sms_row_is_an_error_on_its_column(self, authed_client):
        response = send(authed_client, [row(preferred_contact_method="sms")], dry_run=False)

        body = response.json()
        assert body["rows"][0]["errors"] == [
            {"column": "preferred_contact_method", "message": SMS_UNAVAILABLE}
        ]
        assert stored() == (0, 0)

    def test_a_phone_row_imports(self, authed_client):
        send(authed_client, [row(preferred_contact_method="phone")], dry_run=False)

        assert Customer.objects.get().preferred_contact_method == ContactMethod.PHONE
