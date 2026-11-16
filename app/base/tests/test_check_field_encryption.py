"""
`manage.py check_field_encryption`: do this key and this database belong together?

The command a restore drill ends on (docs/DEPLOY.md). What matters is that it
says no when the key is wrong, that it never shows a code while saying so,
and that it counts every row rather than stopping at the first bad one.
"""

from io import StringIO

import pytest
from cryptography.fernet import Fernet
from django.core.management import CommandError, call_command

from customers.models import ServiceLocation
from scheduling.tests.factories import ServiceLocationFactory

pytestmark = pytest.mark.django_db

GATE, ALARM, KEYS = "4821#", "9930", "Lockbox on the meter, 0412"


def check() -> str:
    out = StringIO()
    call_command("check_field_encryption", stdout=out)
    return out.getvalue()


@pytest.fixture
def location():
    return ServiceLocationFactory(gate_code=GATE, alarm_code=ALARM, key_location=KEYS)


class TestTheRightKey:
    def test_every_stored_value_is_counted_per_column(self, location):
        ServiceLocationFactory(gate_code="1111")

        output = check()

        assert "customers.ServiceLocation.gate_code: 2 readable, 0 unreadable" in output
        assert "customers.ServiceLocation.alarm_code: 1 readable, 0 unreadable" in output
        assert "customers.ServiceLocation.key_location: 1 readable, 0 unreadable" in output
        assert "OK: all 4 encrypted values are readable" in output

    def test_no_code_is_ever_printed(self, location):
        """Its output lands in a terminal's scrollback and a platform's logs."""
        output = check()

        for secret in (GATE, ALARM, KEYS):
            assert secret not in output
        assert "gAAAA" not in output  # nor the ciphertext

    def test_soft_deleted_rows_count_because_the_backup_holds_them_too(self, location):
        location.delete()
        assert not ServiceLocation.objects.exists()

        assert "gate_code: 1 readable" in check()

    def test_a_database_with_no_codes_passes_but_says_it_proved_nothing(self):
        ServiceLocationFactory()

        output = check()

        assert "says nothing about the key" in output
        assert "OK" not in output


class TestTheWrongKey:
    def test_another_valid_key_fails_and_says_how_much_is_lost(self, location, settings):
        settings.FIELD_ENCRYPTION_KEY = Fernet.generate_key().decode()

        with pytest.raises(CommandError, match="3 of 3 encrypted values cannot be read"):
            check()

    def test_it_counts_every_row_rather_than_stopping_at_the_first(self, location, settings):
        """
        Half written with one key and half with another, which is what a key
        replaced without a re-encryption leaves behind (docs/DEPLOY.md,
        "Rotation and recovery").
        """
        settings.FIELD_ENCRYPTION_KEY = Fernet.generate_key().decode()
        ServiceLocationFactory(gate_code="2222")
        out = StringIO()

        with pytest.raises(CommandError, match="3 of 4"):
            call_command("check_field_encryption", stdout=out)

        assert "gate_code: 1 readable, 1 unreadable" in out.getvalue()

    def test_the_failure_does_not_print_a_code_either(self, location, settings):
        settings.FIELD_ENCRYPTION_KEY = Fernet.generate_key().decode()
        out = StringIO()

        with pytest.raises(CommandError) as raised:
            call_command("check_field_encryption", stdout=out)

        for secret in (GATE, ALARM, KEYS):
            assert secret not in out.getvalue() + str(raised.value)

    def test_something_that_is_not_a_key_at_all_is_named_as_that(self, location, settings):
        settings.FIELD_ENCRYPTION_KEY = "not-a-fernet-key"

        with pytest.raises(CommandError, match="not a usable key"):
            check()

    def test_it_writes_nothing(self, location, settings):
        before = ServiceLocation.all_objects.values_list("updated_at", flat=True).get()
        settings.FIELD_ENCRYPTION_KEY = Fernet.generate_key().decode()

        with pytest.raises(CommandError):
            check()

        assert ServiceLocation.all_objects.values_list("updated_at", flat=True).get() == before
