"""Pictures an email links from the web, fetched only when Joe clicks Show pictures on that email.

Loading them tells the sender the email was opened (and from which address), which is why it's a click per email
and never automatic. What keeps it safe for this PC:
- The address comes from the email itself, read again from Outlook on each request (the page only says "the 3rd
  web picture of this email"), and never for a Hoxhunt phishing test.
- http or https on the standard ports, no user:pass@, and every address the name resolves to must be a public
  internet address: an email can't point Command Center at this PC, the LAN or the VPN. The connection goes to the
  address that was checked (no DNS rebinding; https still verifies the certificate against the name). Redirects are
  checked the same way, at most 3. No proxy (it would bypass the check), no cookies, no Referer.
- At most 5 MB, 10 s; only PNG, JPEG, GIF, WebP or BMP as judged from the bytes (mailimages._sniff), never SVG.
- Held in memory only. Logs carry counts and error types, never addresses.
"""
import http.client
import ipaddress
import logging
import socket
import ssl
import urllib.parse
import urllib.request

from . import mailimages

log = logging.getLogger("commandcenter.webimages")

MAX_BYTES = 5 * 1024 * 1024
TIMEOUT = 10
MAX_REDIRECTS = 3
PORTS = {"http": (None, 80), "https": (None, 443)}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


class Refused(ValueError):
    pass


def check_url(url: str) -> str:
    url = (url or "").strip()
    if len(url) > 4000 or any(ord(c) < 33 for c in url):
        raise Refused("bad address")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in PORTS or not parts.hostname or parts.username or parts.password:
        raise Refused("not a web address")
    try:
        port = parts.port
    except ValueError:
        raise Refused("bad port")
    if port not in PORTS[parts.scheme]:
        raise Refused("non-standard port")
    return url


def _public_ips(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise Refused("name not found")
    ips = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            raise Refused("not a public address")
        ips.append(str(ip))
    if not ips:
        raise Refused("name not found")
    return ips


def _connect(conn, default_port):
    if conn._tunnel_host:
        raise Refused("proxy")
    last = None
    for ip in _public_ips(conn.host, conn.port or default_port):
        try:
            return socket.create_connection((ip, conn.port or default_port), conn.timeout, conn.source_address)
        except OSError as e:
            last = e
    raise last or OSError("couldn't connect")


class _PinnedHTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock = _connect(self, 80)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def connect(self):
        sock = _connect(self, 443)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_PinnedHTTP, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PinnedHTTPS, req, context=self._context)


class _Redirects(urllib.request.HTTPRedirectHandler):
    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)  # the connection checks the address again
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            new.remove_header("Referer")
        return new


# No cookie processor, no proxy.
_opener = urllib.request.OpenerDirector()
for _h in (_HTTPHandler(), _HTTPSHandler(context=ssl.create_default_context()), _Redirects(),
           urllib.request.HTTPDefaultErrorHandler(), urllib.request.HTTPErrorProcessor(), urllib.request.UnknownHandler()):
    _opener.add_handler(_h)


def fetch(url: str) -> tuple[bytes, str]:
    """The picture's bytes and type. Raises Refused / OSError / urllib errors."""
    req = urllib.request.Request(check_url(url), headers={
        "User-Agent": UA, "Accept": "image/avif,image/webp,image/png,image/jpeg,image/gif,image/*;q=0.8"})
    with _opener.open(req, timeout=TIMEOUT) as r:
        data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise Refused("too large")
    kind = mailimages._sniff(data)
    if not kind:
        raise Refused("not a picture this can show")
    return data, kind
