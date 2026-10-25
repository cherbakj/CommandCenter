"""Reads the TLS certificate a SQL Server presents, so the Toolkit can pin it.

Some servers (snc-awsw-db01 among them) have no certificate from a CA, only the self-signed one SQL Server makes
for itself ("SSL_Self_Signed_Fallback"), which no CA can vouch for. Instead of trusting any certificate, Joe
approves this one's SHA-256 fingerprint once; before every connection the certificate is read again and the
connection only goes ahead if it is still the same one. SQL Server makes a new fallback certificate when its
service restarts, which then shows up as "certificate changed" for Joe to approve (or not).

How: TDS PRELOGIN asking for encryption, then a TLS handshake carried inside TDS packets (as the ODBC driver
does it). Nothing is sent after the handshake: no login, no password. The check is separate from the ODBC
driver's own connection (which then runs with TrustServerCertificate=yes), so an attacker in the path would
have to let this check through and intercept only the next connection; it still stops anyone who presents
another certificate on every connection.
"""
import hashlib
import socket
import ssl
import struct

from cryptography import x509

TDS_PRELOGIN = 0x12
TIMEOUT = 10


def _endpoint(server: str) -> tuple[str, int]:
    """host, host,port or host\\instance (the instance's port comes from the SQL Browser service)."""
    server = server.strip()
    if server.lower().startswith("tcp:"):
        server = server[4:]
    if "," in server:
        host, port = server.rsplit(",", 1)
        return host.split("\\")[0], int(port)
    if "\\" in server:
        host, instance = server.split("\\", 1)
        return host, _instance_port(host, instance)
    return server, 1433


def _instance_port(host, instance) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(TIMEOUT)
        s.sendto(b"\x04" + instance.encode("ascii", "ignore"), (host, 1434))
        data = s.recv(4096)
    parts = data[3:].decode("ascii", "ignore").split(";")
    for i, p in enumerate(parts[:-1]):
        if p.lower() == "tcp":
            return int(parts[i + 1])
    raise ConnectionError(f"SQL Browser on {host} didn't give a TCP port for instance {instance}")


def _packet(payload: bytes) -> bytes:
    return struct.pack(">BBHHBB", TDS_PRELOGIN, 1, 8 + len(payload), 0, 0, 0) + payload


def _read_packet(sock) -> bytes:
    def exactly(n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("The SQL server closed the connection during the certificate check")
            buf += chunk
        return buf
    length = struct.unpack(">H", exactly(8)[2:4])[0]
    if not 8 <= length <= 32768:
        raise ConnectionError("Unexpected reply from the SQL server")
    return exactly(length - 8)


def fetch(server: str) -> dict:
    """The certificate the server presents now: fingerprint and the fields Joe needs to recognize it."""
    host, port = _endpoint(server)
    with socket.create_connection((host, port), timeout=TIMEOUT) as sock:
        # PRELOGIN: VERSION, ENCRYPTION = ON, terminator.
        opts = [(0x00, b"\x11\x00\x00\x00\x00\x00"), (0x01, b"\x01")]
        offset, head, data = 5 * len(opts) + 1, b"", b""
        for token, value in opts:
            head += struct.pack(">BHH", token, offset + len(data), len(value))
            data += value
        sock.sendall(_packet(head + b"\xff" + data))
        _read_packet(sock)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # the fingerprint below is the check
        incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
        tls = ctx.wrap_bio(incoming, outgoing, server_hostname=host)
        for _ in range(20):
            try:
                tls.do_handshake()
                break
            except ssl.SSLWantReadError:
                if pending := outgoing.read():
                    sock.sendall(_packet(pending))
                incoming.write(_read_packet(sock))
        else:
            raise ConnectionError("The TLS handshake with the SQL server didn't finish")
        der = tls.getpeercert(binary_form=True)
    cert = x509.load_der_x509_certificate(der)
    return {"sha256": hashlib.sha256(der).hexdigest().upper(), "subject": cert.subject.rfc4514_string(),
            "issuer": cert.issuer.rfc4514_string(), "self_signed": cert.subject == cert.issuer,
            "valid_from": cert.not_valid_before_utc.date().isoformat(),
            "valid_to": cert.not_valid_after_utc.date().isoformat(), "host": host, "port": port}
