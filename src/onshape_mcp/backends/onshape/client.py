"""Onshape REST client: counts every request, never retries, keeps Onshape's own error text.

Standard library only. The keys are read from the environment or a file and are never printed or logged.
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode, urljoin, urlsplit

from ... import __version__
from ...budget import Ledger
from ...config import ConfigError

DEFAULT_BASE_URL = "https://cad.onshape.com/api/v10"
USER_AGENT = f"onshape-mcp/{__version__}"
MAX_REDIRECTS = 3
REDIRECTS = (301, 302, 303, 307, 308)

# (method, url, headers, body) -> (status, headers, body)
Transport = Callable[[str, str, dict, bytes | None], tuple[int, dict, bytes]]


class OnshapeError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"Onshape answered {status}: {message}")
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Credentials:
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)

    def header(self) -> str:
        return "Basic " + base64.b64encode(f"{self.access_key}:{self.secret_key}".encode()).decode()


def load_credentials(file: str | None, env=os.environ) -> Credentials:
    """Environment first, then a file holding JSON, KEY=VALUE lines, or a sentence naming both keys."""
    access, secret = env.get("ONSHAPE_ACCESS_KEY"), env.get("ONSHAPE_SECRET_KEY")
    if access and secret:
        return Credentials(access, secret)
    if not file:
        raise ConfigError(
            "No Onshape API keys. Set ONSHAPE_ACCESS_KEY and ONSHAPE_SECRET_KEY, or [backend] credentials_file."
        )
    try:
        text = Path(file).read_text()
    except OSError as e:
        raise ConfigError(f"Cannot read the credentials file {file}: {e.strerror}") from None
    try:
        data = json.loads(text)
        access, secret = data.get("access_key"), data.get("secret_key")
    except (ValueError, AttributeError):
        key = r"[A-Za-z0-9_+/=-]+"
        found_access = re.search(rf"access[ _]key\s*(?:is|:|=)\s*[\"']?({key})", text, re.I)
        found_secret = re.search(rf"secret[ _]key\s*(?:is|:|=)\s*[\"']?({key})", text, re.I)
        access = found_access.group(1) if found_access else None
        secret = found_secret.group(1) if found_secret else None
    if not access or not secret:
        raise ConfigError(f"The credentials file {file} does not name both an access key and a secret key.")
    return Credentials(access, secret)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # redirects come back to us, so each hop is counted and never sent blindly


def urllib_transport(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, dict, bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=120) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


class OnshapeClient:
    def __init__(
        self,
        credentials: Credentials | Callable[[], Credentials],
        ledger: Ledger,
        base_url: str = DEFAULT_BASE_URL,
        transport: Transport = urllib_transport,
    ):
        self._credentials = credentials
        self.ledger = ledger
        self.base_url = base_url.rstrip("/")
        self.transport = transport
        self._host = urlsplit(self.base_url).netloc
        self._base_path = urlsplit(self.base_url).path

    @property
    def credentials(self) -> Credentials:
        """The keys, looked for on first use, so everything that needs no CAD service works without any."""
        if callable(self._credentials):
            self._credentials = self._credentials()
        return self._credentials

    def _trusted(self, parts) -> bool:
        """The keys go to the configured host and its sibling hosts (Onshape serves exports from regional
        servers under the same domain), over HTTPS only, and never to wherever else a redirect points."""
        domain = self._host.split(".", 1)[-1]
        return parts.scheme == "https" and (parts.netloc == self._host or parts.netloc.endswith("." + domain))

    def request(self, method: str, path: str, *, query: dict | None = None, body=None, accept: str = "application/json") -> bytes:
        """Send one request. Each redirect hop counts as a request too. Raises OnshapeError on any failure."""
        url = self.base_url + path + ("?" + urlencode(query) if query else "")
        data = None if body is None else json.dumps(body).encode()
        credentials = self.credentials  # missing keys stop here, before anything is counted or sent
        via = ""
        for _ in range(MAX_REDIRECTS + 1):
            parts = urlsplit(url)
            self.ledger.charge(method, parts.path.removeprefix(self._base_path))
            headers = {"Accept": accept, "User-Agent": USER_AGENT}
            if self._trusted(parts):
                headers["Authorization"] = credentials.header()
            elif via:
                via += ", which is not an Onshape host, so the keys were not sent"
            if data is not None:
                headers["Content-Type"] = "application/json"
            status, response_headers, content = self.transport(method, url, headers, data)
            if status in REDIRECTS:
                location = next((v for k, v in response_headers.items() if k.lower() == "location"), None)
                if method != "GET" or not location:
                    raise OnshapeError(status, "a redirect that is not safe to follow (only GET is followed)")
                url = urljoin(url, location)
                via = f" (after a redirect to {urlsplit(url).netloc}"
                continue
            if not 200 <= status < 300:
                raise OnshapeError(status, content.decode("utf-8", "replace")[:2000] + (via + ")" if via else ""))
            return content
        raise OnshapeError(310, f"more than {MAX_REDIRECTS} redirects")

    def get_json(self, path: str, query: dict | None = None):
        return json.loads(self.request("GET", path, query=query))

    def post_json(self, path: str, body: dict):
        return json.loads(self.request("POST", path, body=body))
