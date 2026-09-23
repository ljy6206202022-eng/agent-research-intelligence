"""Bounded HTTPS reads with validated, pinned DNS and revalidated redirects."""
from dataclasses import dataclass
import http.client
import ipaddress
import json
import re
import socket
import ssl
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

from agent_research_intelligence.governance.paths import BoundaryError


class AcquisitionError(RuntimeError):
    def __init__(self, code: str, *, phase: str | None = None):
        self.code = code
        self.phase = phase
        super().__init__(code)


@dataclass(frozen=True)
class Response:
    url: str
    status: int
    body: bytes
    headers: dict[str, str]
    redirects: tuple[str, ...] = ()


class _PinnedConnection(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        self.phase = "TCP_CONNECT"
        raw = socket.create_connection((self.address, 443), timeout=self.timeout)
        try:
            self.phase = "TLS_HANDSHAKE"
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
            self.phase = "REQUEST_SEND"
        except BaseException:
            raw.close()
            raise


class PublicHTTP:
    _total = threading.BoundedSemaphore(4)
    _host_lock = threading.Lock()
    _hosts = {}

    def __init__(self, *, timeout=20, max_bytes=8 * 1024 * 1024,
                 resolver=socket.getaddrinfo, connection_factory=_PinnedConnection):
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.resolver = resolver
        self.connection_factory = connection_factory
        # Bounded operational diagnostics, never URL queries, bodies or headers.
        self.trace = []
        self.total_requests = 0

    def _destination(self, url):
        try:
            parsed = urlsplit(url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in {None, 443}:
                raise BoundaryError("Only public HTTPS port 443 is allowed")
            host = parsed.hostname.encode("idna").decode().lower().rstrip(".")
            if host == "localhost" or host.endswith(".localhost") or host == "metadata.google.internal":
                raise BoundaryError("Local destination denied")
            records = self.resolver(host, 443, type=socket.SOCK_STREAM)
            addresses = [x[4][0] for x in records]
            if not addresses:
                raise AcquisitionError("DNS_FAILED")
            for address in addresses:
                ip = ipaddress.ip_address(address)
                if not ip.is_global or getattr(ip, "ipv4_mapped", None) is not None:
                    raise BoundaryError("Non-public DNS answer denied")
            return parsed, host, addresses[0]
        except (ValueError, UnicodeError) as exc:
            raise BoundaryError("Malformed URL") from exc
        except socket.gaierror as exc:
            raise AcquisitionError("DNS_FAILED") from exc

    def _record(self, entry):
        self.total_requests += 1
        self.trace.append(entry)
        del self.trace[:-128]

    def get(self, url: str) -> Response:
        return self._read(url)

    def conditional_get(self, url: str, *, etag=None, modified=None) -> Response:
        validators = {}
        for name, value in (("If-None-Match", etag), ("If-Modified-Since", modified)):
            if value is not None:
                if not isinstance(value, str) or not value or len(value)>1024 or any(ord(c)<32 or ord(c)>126 for c in value):
                    raise BoundaryError("Invalid HTTP validator")
                validators[name] = value
        return self._read(url, validators=validators)

    def read_youtube_player(self, video_id: str, public_api_key: str) -> Response:
        """One fixed public metadata-read endpoint; no general POST facility.

        The client key is published in the anonymous watch page, not an account
        credential. The only request data is a validated public video ID and a
        fixed caption-provider client descriptor. No private research input.
        """
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", public_api_key):
            raise BoundaryError("Invalid public player read parameters")
        body = json.dumps({"context": {"client": {"clientName": "ANDROID", "clientVersion": "20.10.38"}},
                           "videoId": video_id}).encode()
        return self._read("https://www.youtube.com/youtubei/v1/player?key=" + public_api_key, player_body=body)

    def _read(self, url: str, *, player_body=None, validators=None) -> Response:
        if player_body is not None:
            if not re.fullmatch(r"https://www\.youtube\.com/youtubei/v1/player\?key=[A-Za-z0-9_-]{1,200}", url):
                raise BoundaryError("POST is limited to the public player read endpoint")
            try:
                payload = json.loads(player_body)
                if (set(payload) != {"context", "videoId"} or
                        payload["context"] != {"client": {"clientName": "ANDROID", "clientVersion": "20.10.38"}} or
                        not re.fullmatch(r"[A-Za-z0-9_-]{11}", payload["videoId"])):
                    raise ValueError("Unexpected player request")
            except (ValueError, TypeError, KeyError) as exc:
                raise BoundaryError("Only a fixed public video lookup body is allowed") from exc
        redirects = []
        validator_origin = urlsplit(url).netloc
        for hop in range(6):
            started = time.monotonic()
            parsed, host, address = self._destination(url)
            with self._host_lock:
                semaphore = self._hosts.setdefault(host, threading.BoundedSemaphore(2))
            with self._total, semaphore:
                connection = self.connection_factory(host, address, self.timeout)
                phase = "REQUEST_SEND"
                entry = {"at": datetime.now(timezone.utc).isoformat(), "host": host,
                         "path": parsed.path or "/", "address": address,
                         "route": getattr(connection, "route", "DIRECT"), "hop": hop}
                try:
                    target = parsed.path or "/"
                    if parsed.query:
                        target += "?" + parsed.query
                    # No ambient cookies, proxy settings or credential headers.
                    headers = {"Accept-Encoding": "identity", "User-Agent": "Research-Intel/0.1 (public research read)"}
                    if validators and parsed.netloc == validator_origin:
                        headers.update(validators)
                    if player_body is None:
                        connection.request("GET", target, headers=headers)
                    else:
                        headers["Content-Type"] = "application/json"
                        connection.request("POST", target, body=player_body, headers=headers)
                    entry["method"] = "GET" if player_body is None else "POST_PLAYER_READ"
                    phase = "RESPONSE_HEADERS"
                    result = connection.getresponse()
                    entry["status"] = result.status
                    headers = {k.lower(): v for k, v in result.getheaders()}
                    if result.status in {301, 302, 303, 307, 308}:
                        if player_body is not None:
                            raise AcquisitionError("PLAYER_READ_REDIRECT_DENIED")
                        if hop == 5 or not headers.get("location"):
                            raise AcquisitionError("REDIRECT_LIMIT_OR_MISSING_LOCATION")
                        redirects.append(url)
                        url = urljoin(url, headers["location"])
                        continue
                    if result.status == 304 and validators and parsed.netloc == validator_origin:
                        entry["bytes"] = 0
                        return Response(url, 304, b"", headers, tuple(redirects))
                    if result.status in {401, 403}:
                        raise AcquisitionError("ACCESS_DENIED")
                    if result.status == 429:
                        error = AcquisitionError("RATE_LIMITED")
                        value = headers.get("retry-after", "")
                        error.retry_after = int(value) if value.isdigit() and len(value)<8 else None
                        raise error
                    if result.status == 404:
                        raise AcquisitionError("NOT_FOUND")
                    if result.status != 200:
                        raise AcquisitionError(f"HTTP_{result.status}")
                    if headers.get("content-encoding", "identity") not in {"", "identity"}:
                        raise AcquisitionError("UNSUPPORTED_CONTENT_ENCODING")
                    phase = "RESPONSE_BODY"
                    body = result.read(self.max_bytes + 1)
                    entry["bytes"] = len(body)
                    if len(body) > self.max_bytes:
                        raise AcquisitionError("CONTENT_TOO_LARGE")
                    return Response(url, result.status, body, headers, tuple(redirects))
                except (socket.timeout, TimeoutError) as exc:
                    phase = getattr(connection, "phase", phase) if phase == "REQUEST_SEND" else phase
                    entry["error"] = "TIMEOUT"
                    raise AcquisitionError("TIMEOUT", phase=phase) from exc
                except (OSError, http.client.HTTPException) as exc:
                    phase = getattr(connection, "phase", phase) if phase == "REQUEST_SEND" else phase
                    entry["error"] = "TRANSPORT_FAILED"
                    raise AcquisitionError("TRANSPORT_FAILED", phase=phase) from exc
                except AcquisitionError as exc:
                    entry["error"] = exc.code
                    if exc.phase is None:
                        exc.phase = phase
                    raise
                finally:
                    connection.close()
                    entry.update(phase=phase, elapsed_s=round(time.monotonic() - started, 4))
                    self._record(entry)
        raise AcquisitionError("REDIRECT_LIMIT")
