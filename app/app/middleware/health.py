"""
Answer the health probes before anything reads the Host header.

An orchestrator probes a process directly, not through the hostname the
public uses: Fly's checker asks for `/health/ready/` with the machine's own
private address as `Host` (`172.19.75.162:8000`), and that address is
different on every machine, so it cannot go in ALLOWED_HOSTS. Left to the
normal stack, CommonMiddleware's host check answers 400, the check reads
critical, and the proxy never routes a request to a perfectly healthy
machine -- which is how the first staging deploy hung.

The host check exists to stop a forged Host reaching anything that builds a
URL from it (password-reset links, redirects). The probes build nothing and
say nothing a stranger could not learn from a 502, so they go first, above
CommonMiddleware, and dispatch straight to the health view. Which paths are
probes is read from the URLconf -- the `health` namespace -- not repeated
here.
"""

from django.urls import Resolver404, resolve


class HealthCheckMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            match = resolve(request.path_info)
        except Resolver404:
            return self.get_response(request)
        if match.namespace == "health":
            return match.func(request, *match.args, **match.kwargs)
        return self.get_response(request)
