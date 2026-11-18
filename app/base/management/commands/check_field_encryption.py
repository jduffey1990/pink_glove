"""
Prove that this process's FIELD_ENCRYPTION_KEY reads this database.

    python manage.py check_field_encryption

The question a restore drill has to answer (docs/DEPLOY.md, "Backup and
restore"): a database came back from a backup, and the key came back from
wherever it is kept. Do they belong together? If they do not, every gate and
alarm code is gone, and without this the first person to find out is a
cleaner standing at a door.

It answers by decrypting every encrypted column of every row and counting.
It never prints a value, decrypted or not: a command that showed a code would
be a way round the audited reveal (ADR-016), and its output lands in a
terminal's scrollback and a platform's logs. It writes nothing and records no
reveal, because no person sees anything.

Exit status is 0 when every value is readable and 1 otherwise, so it can gate
a script.
"""

from cryptography.fernet import InvalidToken
from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db.models import F, TextField
from django.db.models.functions import Cast

from base.fields import EncryptedTextField, _fernet


class Command(BaseCommand):
    help = (
        "Check that every encrypted column decrypts with FIELD_ENCRYPTION_KEY. Prints counts only."
    )

    def handle(self, *args, **options):
        try:
            fernet = _fernet()
        except ValueError as exc:
            # Not a Fernet key at all: wrong length, or not base64.
            raise CommandError(f"FIELD_ENCRYPTION_KEY is not a usable key: {exc}") from exc

        columns = [
            (model, field)
            for model in apps.get_models()
            for field in model._meta.local_fields
            if isinstance(field, EncryptedTextField)
        ]
        if not columns:
            raise CommandError("No model has an encrypted column; there is nothing to check.")

        total = unreadable = 0
        for model, field in columns:
            # `_base_manager`, so soft-deleted rows count: they are in the
            # backup too. Cast to plain text so the column arrives as stored;
            # read through the field, one bad row would raise and hide the rest.
            stored = (
                model._base_manager.exclude(**{field.name: ""})
                .exclude(**{f"{field.name}__isnull": True})
                .annotate(_stored=Cast(F(field.name), TextField()))
                .values_list("_stored", flat=True)
            )
            good = bad = 0
            for ciphertext in stored.iterator():
                try:
                    fernet.decrypt(ciphertext.encode())
                    good += 1
                except InvalidToken:
                    bad += 1
            total += good + bad
            unreadable += bad
            self.stdout.write(
                f"{model._meta.label}.{field.name}: {good} readable, {bad} unreadable"
            )

        if unreadable:
            raise CommandError(
                f"{unreadable} of {total} encrypted values cannot be read with this "
                "FIELD_ENCRYPTION_KEY. It is not the key this database was written with."
            )
        if not total:
            self.stdout.write(
                self.style.WARNING(
                    "No encrypted values are stored, so this says nothing about the key."
                )
            )
            return
        self.stdout.write(
            self.style.SUCCESS(
                f"OK: all {total} encrypted values are readable with this FIELD_ENCRYPTION_KEY."
            )
        )
