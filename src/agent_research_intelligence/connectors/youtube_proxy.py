"""User-approved source transport exception, not a general proxy facility.

Only explicit opt-in and the exact approved loopback endpoint are accepted.
YouTube addresses are resolved through authenticated HTTPS to Google Public DNS,
then CONNECT pins the checked IP while TLS verifies the original hostname.
All other research hosts retain the original direct transport. No environment,
system proxy configuration, cookies, credentials or browser state are consulted.
"""
import ipaddress
import json
import socket
from urllib.parse import quote

from agent_research_intelligence.governance.paths import BoundaryError
from .public_http import AcquisitionError, PublicHTTP, _PinnedConnection

APPROVED_PROXY = "http://127.0.0.1:9674"
PROXY_ENDPOINT = ("127.0.0.1", 9674)
YOUTUBE_HOSTS = frozenset({"www.youtube.com", "youtube.com", "m.youtube.com", "youtu.be"})


class _ProxyPinnedConnection(_PinnedConnection):
    route = "EXPLICIT_SOURCE_PROXY"

    def connect(self):
        # The destination is already checked by PublicHTTP. Check here as well
        # so this narrow connection cannot tunnel to a local service directly.
        address = ipaddress.ip_address(self.address)
        if not address.is_global or getattr(address, "ipv4_mapped", None) is not None:
            raise BoundaryError("Non-public proxy destination denied")
        self.set_tunnel(self.address, 443)
        self.phase = "PROXY_TCP_CONNECT"
        self.sock = socket.create_connection(PROXY_ENDPOINT, timeout=self.timeout)
        try:
            self.phase = "PROXY_CONNECT_TUNNEL"
            self._tunnel()
            self.phase = "TLS_HANDSHAKE"
            self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)
            self.phase = "REQUEST_SEND"
        except BaseException:
            self.close()
            raise


def _bootstrap(host, port, **kwargs):
    if host != "dns.google" or port != 443:
        raise BoundaryError("DNS bootstrap cannot redirect to another host")
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]


class YouTubeProxyHTTP(PublicHTTP):
    def __init__(self, proxy_url: str, *, timeout=20, max_bytes=8 * 1024 * 1024):
        if proxy_url != APPROVED_PROXY:
            raise BoundaryError("Only the explicitly approved source proxy endpoint is allowed")
        self.dns = PublicHTTP(timeout=timeout, max_bytes=64 * 1024,
                              resolver=_bootstrap, connection_factory=_ProxyPinnedConnection)
        super().__init__(timeout=timeout, max_bytes=max_bytes,
                         resolver=self._resolve, connection_factory=self._connection)

    @staticmethod
    def _proxy_host(host):
        return host in YOUTUBE_HOSTS

    def _connection(self, host, address, timeout):
        factory = _ProxyPinnedConnection if self._proxy_host(host) else _PinnedConnection
        return factory(host, address, timeout)

    def _resolve(self, host, port, **kwargs):
        if not self._proxy_host(host):
            return socket.getaddrinfo(host, port, **kwargs)
        url = "https://dns.google/resolve?name=" + quote(host, safe="") + "&type=A&edns_client_subnet=0.0.0.0/0"
        try:
            response = self.dns.get(url)
            value = json.loads(response.body)
            if (value.get("Status") != 0 or value.get("TC") is True or
                    value.get("Question") != [{"name": host + ".", "type": 1}]):
                raise ValueError("Unusable DNS response")
            addresses = [x["data"] for x in value.get("Answer", []) if x.get("type") == 1]
            if not addresses:
                raise ValueError("No A records")
            for raw in addresses:
                address = ipaddress.ip_address(raw)
                if address.version != 4 or not address.is_global:
                    raise BoundaryError("Non-public proxy DNS answer denied")
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)) for address in addresses]
        except BoundaryError:
            raise
        except AcquisitionError as exc:
            raise AcquisitionError("PROXY_DNS_" + exc.code, phase="DNS_OVER_HTTPS") from exc
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise AcquisitionError("PROXY_DNS_INVALID_RESPONSE", phase="DNS_OVER_HTTPS") from exc

    def diagnostics(self):
        return {"transport": list(self.trace), "dns": list(self.dns.trace)}
