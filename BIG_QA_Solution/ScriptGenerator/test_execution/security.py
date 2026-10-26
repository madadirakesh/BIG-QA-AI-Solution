"""
Guards applied before anything from an uploaded sheet, Jira or Zephyr reaches
the browser or the filesystem. Test step text is untrusted input: it can only
ever become one of the actions in models.ACTION_SCHEMA, never code or a shell
command, and it can only navigate to hosts the run explicitly allows.
"""
from __future__ import annotations

import fnmatch
import ipaddress
import os
import re
import socket
from urllib.parse import urljoin, urlparse

MAX_UPLOAD_BYTES = int(os.environ.get("TEST_EXECUTION_MAX_UPLOAD_MB") or "15") * 1024 * 1024
ALLOWED_UPLOAD_EXTENSIONS = {".xlsx", ".xls"}

# Always blocked, whatever the allowlist says: cloud metadata services.
_METADATA_HOSTS = {
    "169.254.169.254", "metadata.google.internal", "metadata", "100.100.100.200",
    "fd00:ec2::254", "instance-data", "instance-data.ec2.internal",
}

# Magic numbers: xlsx is a zip container, xls is an OLE2 compound document.
_ZIP_MAGIC = b"PK\x03\x04"
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


class PolicyError(ValueError):
    """A request the security policy refuses. The message is safe to show."""


def sanitize_filename(name: str, default: str = "file") -> str:
    base = os.path.basename(str(name or "").replace("\\", "/"))
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._")
    return (base or default)[:120]


def validate_upload(filename: str, data: bytes) -> str:
    """Returns the lower-case extension, or raises PolicyError."""
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        raise PolicyError("Only .xlsx and .xls workbooks are supported.")
    if not data:
        raise PolicyError("The uploaded file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise PolicyError(f"The file exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit.")
    if ext == ".xlsx" and not data.startswith(_ZIP_MAGIC):
        raise PolicyError("The file is not a valid .xlsx workbook.")
    if ext == ".xls" and not data.startswith(_OLE_MAGIC):
        raise PolicyError("The file is not a valid .xls workbook.")
    return ext


def safe_join(root: str, *parts: str) -> str:
    """Join and refuse anything that would escape `root` (path traversal)."""
    root_abs = os.path.realpath(root)
    path = os.path.realpath(os.path.join(root_abs, *parts))
    if path != root_abs and not path.startswith(root_abs + os.sep):
        raise PolicyError("Path escapes the storage root.")
    return path


def _host_is_metadata(host: str) -> bool:
    h = (host or "").strip("[]").lower()
    if h in _METADATA_HOSTS:
        return True
    try:
        ip = ipaddress.ip_address(h)
        return ip.is_link_local  # 169.254.0.0/16, fe80::/10
    except ValueError:
        return False


def _resolves_to_link_local(host: str) -> bool:
    """DNS names that point at link-local addresses (e.g. a rebinding trick)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        try:
            if ipaddress.ip_address(info[4][0]).is_link_local:
                return True
        except ValueError:
            continue
    return False


def host_allowed(host: str, allowed_hosts: list[str]) -> bool:
    host = (host or "").lower()
    for pattern in allowed_hosts:
        pattern = pattern.lower()
        if host == pattern or fnmatch.fnmatch(host, pattern):
            return True
    return False


class NavigationPolicy:
    """
    Decides which URLs the browser may open. The base URL's host is always
    allowed; extra hosts come from the run config and the
    TEST_EXECUTION_ALLOWED_HOSTS env var (comma separated, wildcards allowed).
    """

    def __init__(self, base_url: str, extra_hosts: list[str] | None = None, resolve_dns: bool = True):
        self.base_url = (base_url or "").strip()
        self.resolve_dns = resolve_dns
        hosts = [h.strip().lower() for h in (extra_hosts or []) if h.strip()]
        env_hosts = os.environ.get("TEST_EXECUTION_ALLOWED_HOSTS", "")
        hosts += [h.strip().lower() for h in env_hosts.split(",") if h.strip()]
        if self.base_url:
            parsed = urlparse(self.base_url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise PolicyError("Base URL must be an http(s) URL.")
            hosts.append(parsed.hostname.lower())
        self.allowed_hosts = sorted(set(hosts))

    def resolve(self, url_or_path: str) -> str:
        """Turns a step's URL/path into an absolute URL, or raises PolicyError."""
        raw = (url_or_path or "").strip().strip("'\"")
        if not raw:
            raise PolicyError("No URL given.")
        if raw.startswith("/") or not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", raw):
            if not raw.startswith("/") and "." in raw.split("/")[0] and " " not in raw:
                raw = "https://" + raw    # "www.example.com/login"
            elif not self.base_url:
                raise PolicyError(f"'{raw}' is a relative path but no base URL is configured.")
            elif raw == "/":
                raw = self.base_url       # "/" means the application's entry URL
            else:
                raw = urljoin(self.base_url, raw)
        self.check(raw)
        return raw

    def check(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise PolicyError(f"Navigation to '{parsed.scheme}:' URLs is not allowed.")
        host = (parsed.hostname or "").lower()
        if not host:
            raise PolicyError("URL has no host.")
        if _host_is_metadata(host) or (self.resolve_dns and _resolves_to_link_local(host)):
            raise PolicyError(f"Navigation to {host} is blocked (cloud metadata / link-local).")
        if not host_allowed(host, self.allowed_hosts):
            raise PolicyError(
                f"Navigation to {host} is not allowed. Add it to Allowed Hosts or "
                "TEST_EXECUTION_ALLOWED_HOSTS if it is an approved test environment."
            )

    @staticmethod
    def blocked_origins_arg() -> str:
        """
        Playwright MCP --blocked-origins: stops even sub-resource requests to metadata services.
        (--allowed-origins is not used: it would also block the CDNs real applications load
        assets from, and Playwright documents it as not being a security boundary. The
        navigation boundary is check() plus the engine's post-redirect landing check.)
        """
        hosts = [h for h in sorted(_METADATA_HOSTS) if ":" not in h]
        return ";".join(f"{scheme}://{h}" for h in hosts for scheme in ("http", "https"))


def is_production_url(url: str) -> bool:
    """Hosts matching TEST_EXECUTION_PRODUCTION_HOSTS need explicit confirmation to run against."""
    patterns = [p.strip().lower() for p in os.environ.get("TEST_EXECUTION_PRODUCTION_HOSTS", "").split(",") if p.strip()]
    host = (urlparse(url or "").hostname or "").lower()
    return bool(host) and host_allowed(host, patterns)


class Redactor:
    """Masks known secret values in any text that leaves the engine (logs, results, AI prompts)."""

    def __init__(self, secrets: list[str] | None = None):
        self._secrets = sorted({s for s in (secrets or []) if s and len(s) >= 3}, key=len, reverse=True)

    def add(self, value: str) -> None:
        if value and len(value) >= 3 and value not in self._secrets:
            self._secrets.append(value)
            self._secrets.sort(key=len, reverse=True)

    def __call__(self, text) -> str:
        if text is None:
            return ""
        out = str(text)
        for s in self._secrets:
            out = out.replace(s, "******")
        # Never echo bearer/basic tokens that show up in error messages.
        out = re.sub(r"(?i)(authorization:\s*)(bearer|basic)\s+\S+", r"\1\2 ******", out)
        return out
