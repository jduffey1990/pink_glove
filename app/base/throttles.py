"""Throttles that are keyed on something other than the caller's IP."""

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
