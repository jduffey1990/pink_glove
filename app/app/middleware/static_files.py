"""
Whitenoise, taught the shape of Vite's asset names.

Whitenoise caches a file forever only when it can prove the name carries a
content hash. Its own test recognises what ManifestStaticFilesStorage writes
under /static/ (`base.3f2a1b9c8d7e.css`). The frontend build served from
`WHITENOISE_ROOT` (ADR-027) hashes differently -- `assets/index-DGeD55ZR.js`,
a dash and eight base64url characters -- so without this every visit
re-fetched the whole app every sixty seconds. Nothing outside `assets/` is
hashed (favicon, `layers.css`, the index itself), and those keep the default.
"""

import re

from whitenoise.middleware import WhiteNoiseMiddleware as _WhiteNoiseMiddleware

_VITE_HASHED = re.compile(r"^/assets/.+-[A-Za-z0-9_-]{8,}\.\w+$")


class WhiteNoiseMiddleware(_WhiteNoiseMiddleware):
    def immutable_file_test(self, path, url):
        return bool(_VITE_HASHED.match(url)) or super().immutable_file_test(path, url)
