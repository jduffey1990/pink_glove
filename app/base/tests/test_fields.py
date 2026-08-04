"""Tests for EncryptedTextField, using ServiceLocation as the concrete carrier."""

import pytest
from django.db import connection


@pytest.fixture
def location(db, organization):
    from customers.models import Customer, ServiceLocation

    customer = Customer.objects.create(organization=organization, first_name="Dana", last_name="Wu")
    return ServiceLocation.objects.create(
        organization=organization,
        customer=customer,
        line1="1 Elm St",
        city="Denver",
        state="CO",
        postal_code="80202",
        gate_code="4821#",
        alarm_code="9930",
    )


@pytest.mark.django_db
class TestEncryptedTextField:
    def test_roundtrips_through_the_database(self, location):
        location.refresh_from_db()

        assert location.gate_code == "4821#"
        assert location.alarm_code == "9930"

    def test_the_plaintext_is_not_in_the_stored_column(self, location):
        """The whole point: a database dump must not yield the codes."""
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT gate_code, alarm_code FROM customers_servicelocation WHERE id = %s",
                [str(location.id)],
            )
            stored_gate, stored_alarm = cursor.fetchone()

        assert "4821#" not in stored_gate
        assert "9930" not in stored_alarm
        assert stored_gate.startswith("gAAAAA")  # Fernet token prefix

    def test_the_same_value_encrypts_differently_each_time(self, organization, location):
        """
        Fernet includes a random IV, so identical plaintext yields different
        ciphertext. This is why encrypted columns cannot be filtered on.
        """
        from customers.models import ServiceLocation

        second = ServiceLocation.objects.create(
            organization=organization,
            customer=location.customer,
            line1="2 Elm St",
            city="Denver",
            state="CO",
            postal_code="80202",
            gate_code="4821#",
        )

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT gate_code FROM customers_servicelocation WHERE id IN (%s, %s)",
                [str(location.id), str(second.id)],
            )
            stored = [row[0] for row in cursor.fetchall()]

        assert stored[0] != stored[1]
        assert location.gate_code == second.gate_code == "4821#"

    def test_blank_values_pass_through(self, organization, location):
        from customers.models import ServiceLocation

        plain = ServiceLocation.objects.create(
            organization=organization,
            customer=location.customer,
            line1="3 Elm St",
            city="Denver",
            state="CO",
            postal_code="80202",
        )
        plain.refresh_from_db()

        assert plain.gate_code == ""
        assert plain.key_location == ""

    def test_a_wrong_key_raises_rather_than_returning_garbage(self, location, settings):
        import base64

        from customers.models import ServiceLocation

        settings.FIELD_ENCRYPTION_KEY = base64.urlsafe_b64encode(
            b"a-different-key-32-bytes-long!!!"
        ).decode()

        with pytest.raises(ValueError, match="Could not decrypt"):
            _ = ServiceLocation.objects.get(pk=location.pk).gate_code

    def test_missing_key_is_configuration_error(self, location, settings):
        from django.core.exceptions import ImproperlyConfigured

        from customers.models import ServiceLocation

        settings.FIELD_ENCRYPTION_KEY = ""

        with pytest.raises(ImproperlyConfigured, match="FIELD_ENCRYPTION_KEY"):
            _ = ServiceLocation.objects.get(pk=location.pk).gate_code
