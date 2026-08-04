"""
Application-level encrypted fields.

Used for the small number of columns where a database dump is not merely a
privacy problem but a physical-security one: gate codes, alarm codes, key-box
combinations. Everything else stays plaintext -- encryption costs
searchability, so it is applied deliberately rather than broadly.

Trade-offs, all of them real:

- **Encrypted columns cannot be filtered, ordered, or indexed on.** Ciphertext
  differs every time even for identical input. Never put one in a lookup.
- **Losing FIELD_ENCRYPTION_KEY loses the data.** It belongs in your secret
  store and your backups, not only in the app's environment.
- This protects a stolen database dump. It does NOT protect against an
  attacker who already has application-level access, because the running app
  necessarily holds the key.
"""

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models


def _fernet() -> Fernet:
    key = getattr(settings, "FIELD_ENCRYPTION_KEY", "")
    if not key:
        raise ImproperlyConfigured(
            "FIELD_ENCRYPTION_KEY is not set, so encrypted fields cannot be read "
            "or written. Generate one with:\n"
            "  python -c 'from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())'"
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


class EncryptedTextField(models.TextField):
    """
    Transparently encrypts on write and decrypts on read.

    Stored as Fernet ciphertext (AES-128-CBC with an HMAC), so tampering is
    detected rather than silently decrypting to garbage.
    """

    description = "Text, encrypted at rest"

    def get_prep_value(self, value):
        if value is None or value == "":
            return value
        return _fernet().encrypt(str(value).encode()).decode()

    def from_db_value(self, value, expression, connection):
        if value is None or value == "":
            return value
        try:
            return _fernet().decrypt(value.encode()).decode()
        except InvalidToken as exc:
            raise ValueError(
                f"Could not decrypt {self.model._meta.label}.{self.name}. "
                "FIELD_ENCRYPTION_KEY has probably changed since this row was "
                "written."
            ) from exc
