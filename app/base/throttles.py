"""Throttles that are keyed on something other than the caller's IP."""

import hashlib
from collections.abc import Mapping

from rest_framework.throttling import SimpleRateThrottle


class LoginAccountThrottle(SimpleRateThrottle):
    """
    Limits sign-in attempts per *account*, from anywhere.

    The IP-keyed throttles stop one machine hammering the endpoint. They do
    nothing about a guesser spread over many addresses working on one account,
    which is the attack that actually matters for a password. The price is that
    a stranger can use up someone's attempts for the hour; the rate is set high
    enough that this is a nuisance rather than a lockout.
    """

    scope = "login_account"

    def get_cache_key(self, request, view):
        email = (request.data.get("email") or "").strip().lower()
        if not email:
            return None
        return self.cache_format % {"scope": self.scope, "ident": email}


class PasswordResetAccountThrottle(SimpleRateThrottle):
    """
    Limits reset emails per *address*, from anywhere.

    Each request for a staff address puts an email in that person's mailbox,
    so without this a stranger rotating IPs could bury someone in them. It
    counts every address the same way, staff or not, known or not: a 429
    that only appeared for real accounts would say which ones are real.

    Using up someone's allowance does not lock them out. The requests that
    used it up each sent them a working link, and the newest one stays good.
    """

    scope = "password_reset_account"

    def get_cache_key(self, request, view):
        # Runs before the serializer has looked at the body, so the body can
        # be anything at all: a list, a number where the address should be.
        data = request.data
        email = data.get("email") if isinstance(data, Mapping) else None
        if not isinstance(email, str) or not email.strip():
            return None
        # Hashed so an address of any length or alphabet is a valid cache key.
        ident = hashlib.sha256(email.strip().lower().encode()).hexdigest()
        return self.cache_format % {"scope": self.scope, "ident": ident}
