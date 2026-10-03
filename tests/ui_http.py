"""Authenticated client for endpoint tests; security tests use raw HTTP instead."""

import re
from urllib.parse import urlsplit
from urllib.request import Request, urlopen as raw_urlopen


def authenticated_request(url, timeout=10):
    request = Request(url) if isinstance(url, str) else url
    parts = urlsplit(request.full_url)
    if parts.path == "/api" or parts.path.startswith("/api/"):
        # Use the public page bootstrap, just as the browser does. No shared cache
        # or patched authentication gate can hide a missing token in production.
        with raw_urlopen(f"{parts.scheme}://{parts.netloc}/", timeout=timeout) as page:
            token = re.search(rb'name="kumosql-session-token" content="([^"]+)"', page.read())[1].decode("ascii")
        request.add_header("X-KumoSQL-Session", token)
    return request


def urlopen(url, *args, **kwargs):
    return raw_urlopen(authenticated_request(url, timeout=kwargs.get("timeout", 10)), *args, **kwargs)
