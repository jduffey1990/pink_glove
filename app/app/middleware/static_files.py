"""
Whitenoise, taught the shape of Vite's asset names.

Whitenoise caches a file forever only when it can prove the name carries a
content hash. Its own test recognises what ManifestStaticFilesStorage writes
under /static/ (`base.3f2a1b9c8d7e.css`). The frontend build served from
`WHITENOISE_ROOT` (ADR-027) hashes differently -- `assets/index-DGeD55ZR.js`,
a dash and eight base64url characters -- so without this every visit
re-fetched the whole app every sixty seconds. Nothing outside `assets/` is
hashed (favicon, `layers.css`), and those keep the default. `index.html` is not
served from here at all -- it is the SPA's document and belongs to `app.spa`.
"""

import re

from whitenoise.middleware import WhiteNoiseMiddleware as _WhiteNoiseMiddleware

# Vite's default hash is exactly eight base64url characters after a dash.
_VITE_HASHED = re.compile(r"^/assets/.+-[A-Za-z0-9_-]{8}\.\w+$")


class WhiteNoiseMiddleware(_WhiteNoiseMiddleware):
    def add_file_to_dictionary(self, url, path, stat_cache=None):
        # The SPA's document is served by app.spa, never as a file: that view
        # sets Cache-Control: no-cache and runs the rest of the middleware
        # (X-Frame-Options among it); whitenoise would do neither.
        if url == "/index.html":
            return
        super().add_file_to_dictionary(url, path, stat_cache=stat_cache)

    def immutable_file_test(self, path, url):
        return bool(_VITE_HASHED.match(url)) or super().immutable_file_test(path, url)
