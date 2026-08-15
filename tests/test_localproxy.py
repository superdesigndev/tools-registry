"""The local proxy (`src/treg/localproxy.py`) — P0 (skeleton, blind tunnel) and P1 (certificates).

Everything here runs against loopback sockets; nothing reaches the internet and nothing needs a treg
server. Two properties are worth naming, because they are what makes the feature safe to install
before interception exists (P2):

- a connection to a host we do not intercept is copied **byte for byte** and no certificate is ever
  generated for it;
- the trust bundle we hand the agent is the system roots **plus** ours, never ours alone.

See docs/LOCAL-PROXY-PLAN.md §Testing.
"""

from __future__ import annotations

import asyncio
import base64
import os
import socket
import ssl
import stat
import threading
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from treg import localproxy as lp


# ---- helpers ------------------------------------------------------------------------------
class _EchoServer:
    """A plain TCP server that echoes back whatever it is sent, upper-cased. Stands in for "the real
    upstream" so a tunnel can be proved byte-faithful without the network."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        with conn:
            while True:
                try:
                    data = conn.recv(4096)
                except OSError:
                    return
                if not data:
                    return
                conn.sendall(data.upper())

    def close(self):
        self.sock.close()


def _proxy_connect(port: int, token: str, target: str) -> socket.socket:
    """Open a CONNECT tunnel through the proxy and return the socket, positioned after the reply."""
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    cred = base64.b64encode(f"treg:{token}".encode()).decode()
    s.sendall(
        f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
        f"Proxy-Authorization: Basic {cred}\r\n\r\n".encode()
    )
    return s


def _read_head(s: socket.socket) -> bytes:
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf


@pytest.fixture()
def proxy():
    """A running proxy on an operating-system-assigned port, torn down after the test."""
    handle = lp.start(lp.ProxyConfig(token=lp.mint_token(), port=0))
    yield handle
    handle.stop()


@pytest.fixture()
def ca(tmp_path):
    return lp.ensure_ca(tmp_path / "proxy")


# ---- P0 · listening, authentication, blind tunnel ----------------------------------------
def test_binds_loopback_only(proxy):
    """Rule 4: the proxy speaks for the member's quota, so it must never be reachable off-box."""
    host = proxy._server.sockets[0].getsockname()[0]
    assert host == "127.0.0.1"


def test_tunnel_is_byte_faithful(proxy):
    """A CONNECT tunnel copies bytes both ways without touching them."""
    echo = _EchoServer()
    try:
        s = _proxy_connect(proxy.port, proxy.token, f"127.0.0.1:{echo.port}")
        assert _read_head(s).startswith(b"HTTP/1.1 200 ")
        payload = b"hello \x00\xff binary bytes"
        s.sendall(payload)
        assert s.recv(4096) == payload.upper()
        s.close()
    finally:
        echo.close()


def test_tunnel_generates_no_certificate(proxy, ca):
    """The point of P0, and the standing promise for any non-registered host: we never decrypt it, so
    no leaf certificate is ever signed for it."""
    proxy.ca = ca
    echo = _EchoServer()
    try:
        s = _proxy_connect(proxy.port, proxy.token, f"127.0.0.1:{echo.port}")
        _read_head(s)
        s.sendall(b"ping")
        s.recv(4096)
        s.close()
    finally:
        echo.close()
    assert ca._contexts == {}


def test_missing_credentials_get_407(proxy):
    """Rule 5: without the session token the proxy answers 407 and does nothing else — another local
    process cannot quietly spend the member's quota through us."""
    s = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
    s.sendall(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com\r\n\r\n")
    head = _read_head(s)
    assert head.startswith(b"HTTP/1.1 407 ")
    assert b"Proxy-Authenticate: Basic" in head
    s.close()


def test_wrong_token_is_rejected(proxy):
    s = _proxy_connect(proxy.port, "not-the-token", "example.com:443")
    assert _read_head(s).startswith(b"HTTP/1.1 407 ")
    s.close()


def test_error_text_never_leaks_the_token(proxy):
    s = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
    s.sendall(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
    body = _read_head(s) + s.recv(4096)
    assert proxy.token.encode() not in body
    s.close()


def test_refuses_to_tunnel_to_itself(proxy):
    """A loop guard: pointing the proxy at its own address would eat a connection per attempt."""
    s = _proxy_connect(proxy.port, proxy.token, f"127.0.0.1:{proxy.port}")
    assert _read_head(s).startswith(b"HTTP/1.1 400 ")
    s.close()


def test_unreachable_upstream_is_a_readable_502(proxy):
    """A treg-side or network failure must not read like the vendor being down."""
    with socket.socket() as probe:                 # a port nobody is listening on
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
    s = _proxy_connect(proxy.port, proxy.token, f"127.0.0.1:{dead_port}")
    head = _read_head(s)
    assert head.startswith(b"HTTP/1.1 502 ")
    assert b"treg proxy" in head + s.recv(4096)
    s.close()


def test_malformed_request_is_a_400(proxy):
    s = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
    s.sendall(b"GARBAGE\r\n\r\n")
    assert _read_head(s).startswith(b"HTTP/1.1 400 ")
    s.close()


def test_relative_form_request_is_refused(proxy):
    """A request that is not CONNECT and not absolute-form did not come through a proxy setting."""
    cred = base64.b64encode(f"treg:{proxy.token}".encode()).decode()
    s = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
    s.sendall(f"GET /health HTTP/1.1\r\nHost: x\r\nProxy-Authorization: Basic {cred}\r\n\r\n".encode())
    assert _read_head(s).startswith(b"HTTP/1.1 400 ")
    s.close()


def test_plain_http_is_forwarded(proxy):
    """`HTTP_PROXY` is set alongside `HTTPS_PROXY`, so an http:// caller must keep working."""
    served = threading.Event()
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    seen: list[bytes] = []

    def _serve():
        conn, _ = srv.accept()
        with conn:
            seen.append(conn.recv(4096))
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        served.set()

    threading.Thread(target=_serve, daemon=True).start()
    cred = base64.b64encode(f"treg:{proxy.token}".encode()).decode()
    s = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
    s.sendall(
        f"GET http://127.0.0.1:{port}/thing?a=1 HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
        f"Proxy-Authorization: Basic {cred}\r\nConnection: close\r\n\r\n".encode()
    )
    reply = _read_head(s)
    s.close()
    srv.close()
    assert served.wait(timeout=5)
    assert reply.startswith(b"HTTP/1.1 200 ")
    # Origin-form request line, and our session token stripped before the upstream ever sees it.
    assert seen[0].startswith(b"GET /thing?a=1 HTTP/1.1\r\n")
    assert b"Proxy-Authorization" not in seen[0]


# ---- P0 · unit pieces ---------------------------------------------------------------------
def test_authorized_accepts_only_the_exact_token():
    ok = {"proxy-authorization": "Basic " + base64.b64encode(b"treg:abc").decode()}
    assert lp.authorized(ok, "abc")
    assert not lp.authorized(ok, "abcd")
    assert not lp.authorized({}, "abc")
    assert not lp.authorized({"proxy-authorization": "Bearer abc"}, "abc")
    assert not lp.authorized({"proxy-authorization": "Basic !!!not-base64"}, "abc")


def test_split_hostport_handles_ipv6_and_defaults():
    assert lp.split_hostport("api.stripe.com:443", 443) == ("api.stripe.com", 443)
    assert lp.split_hostport("api.stripe.com", 443) == ("api.stripe.com", 443)
    assert lp.split_hostport("[::1]:8443", 443) == ("::1", 8443)
    assert lp.split_hostport("[::1]", 443) == ("::1", 443)


def test_mint_token_is_unguessable():
    assert len({lp.mint_token() for _ in range(50)}) == 50
    assert len(lp.mint_token()) >= 24


# ---- P1 · the certificate authority --------------------------------------------------------
def test_ensure_ca_writes_a_private_key_only_the_owner_can_read(tmp_path):
    """Rule 1. A CA private key readable by other accounts on the machine would let them impersonate
    any site to this user."""
    ca = lp.ensure_ca(tmp_path / "proxy")
    assert stat.S_IMODE(os.stat(ca.key_path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(ca.cert_path).st_mode) == 0o644
    assert stat.S_IMODE(os.stat(ca.dir).st_mode) == 0o700


def test_ca_is_a_two_year_signing_certificate(ca):
    from cryptography import x509

    basic = ca.cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    usage = ca.cert.extensions.get_extension_for_class(x509.KeyUsage).value
    assert basic.ca is True and basic.path_length == 0
    assert usage.key_cert_sign is True
    life = ca.expires - datetime.now(timezone.utc)
    assert timedelta(days=720) < life <= timedelta(days=lp.CA_DAYS)
    assert ca.cert.subject == ca.cert.issuer          # self-signed root


def test_ca_is_reused_across_starts(tmp_path):
    """The agent trusts the bundle for the whole session and beyond; regenerating on every start
    would invalidate it constantly."""
    first = lp.ensure_ca(tmp_path / "proxy")
    second = lp.ensure_ca(tmp_path / "proxy")
    assert first.cert.serial_number == second.cert.serial_number


def test_renew_replaces_the_authority(tmp_path):
    first = lp.ensure_ca(tmp_path / "proxy")
    renewed = lp.ensure_ca(tmp_path / "proxy", renew=True)
    assert renewed.cert.serial_number != first.cert.serial_number
    assert renewed.expires > first.cert.not_valid_before_utc


def test_an_expiring_ca_is_regenerated(tmp_path):
    """Inside the last 30 days we replace it, rather than let an expiry surface mid-session as an
    unexplained TLS error."""
    old = lp.ensure_ca(tmp_path / "proxy", days=10)
    fresh = lp.ensure_ca(tmp_path / "proxy")
    assert fresh.cert.serial_number != old.cert.serial_number
    assert fresh.expires - datetime.now(timezone.utc) > timedelta(days=700)


def test_a_corrupt_ca_does_not_brick_the_proxy(tmp_path):
    ca = lp.ensure_ca(tmp_path / "proxy")
    ca.key_path.write_text("not a key")
    again = lp.ensure_ca(tmp_path / "proxy")
    assert again.cert.serial_number != ca.cert.serial_number


def test_bundle_is_the_system_roots_plus_ours(ca):
    """Rule: `SSL_CERT_FILE` REPLACES the trust list. A bundle holding only our CA would leave the
    agent unable to verify the real internet."""
    bundle = ca.bundle_path.read_bytes()
    assert bundle.endswith(ca.cert_path.read_bytes())
    assert bundle.count(b"-----BEGIN CERTIFICATE-----") > 10   # the system roots are still there
    assert stat.S_IMODE(os.stat(ca.bundle_path).st_mode) == 0o644


def test_leaf_carries_the_host_in_its_subject_alternative_name(ca):
    from cryptography import x509

    cert_pem, key_pem = ca.leaf_pem("api.stripe.com")
    leaf = x509.load_pem_x509_certificate(cert_pem)
    names = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert names.get_values_for_type(x509.DNSName) == ["api.stripe.com"]
    assert leaf.issuer == ca.cert.subject
    assert b"PRIVATE KEY" in key_pem


def test_leaf_for_an_ip_uses_an_ip_entry(ca):
    import ipaddress

    from cryptography import x509

    cert_pem, _ = ca.leaf_pem("127.0.0.1")
    leaf = x509.load_pem_x509_certificate(cert_pem)
    names = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert names.get_values_for_type(x509.IPAddress) == [ipaddress.ip_address("127.0.0.1")]


def test_context_is_cached_per_host(ca):
    """A fresh signature per call would put a public-key operation in the hot path."""
    first = ca.context_for("api.stripe.com")
    assert ca.context_for("api.stripe.com") is first
    assert ca.context_for("api.openai.com") is not first


def test_no_leaf_key_is_left_on_disk(ca):
    """`load_cert_chain` only reads from a file, so the leaf touches disk for microseconds. Nothing
    may remain afterwards."""
    ca.context_for("api.stripe.com")
    leftovers = [p.name for p in ca.dir.iterdir() if p.name.startswith("treg-leaf-")]
    assert leftovers == []


def test_a_real_client_trusts_a_leaf_signed_by_our_ca(ca):
    """The proof that P1 works end to end: a TLS client that trusts only our bundle completes a
    handshake with a server presenting a leaf we signed for that hostname."""
    server_ctx = ca.context_for("api.stripe.com")
    client_ctx = _trusting(ca)

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    result: list = []

    def _serve():
        conn, _ = listener.accept()
        try:
            with server_ctx.wrap_socket(conn, server_side=True) as tls:
                tls.sendall(b"hello")
        except ssl.SSLError as exc:                       # pragma: no cover - failure path
            result.append(exc)

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
            with client_ctx.wrap_socket(raw, server_hostname="api.stripe.com") as tls:
                assert tls.recv(16) == b"hello"
    finally:
        thread.join(timeout=5)
        listener.close()
    assert result == []


def test_a_client_trusting_only_the_system_roots_rejects_our_leaf(ca):
    """The other half of the same promise: our certificates are trusted ONLY where we explicitly put
    the bundle. Rule 2 — the system trust store is never touched."""
    server_ctx = ca.context_for("api.stripe.com")
    client_ctx = ssl.create_default_context()             # system roots only
    client_ctx.minimum_version = ssl.TLSVersion.TLSv1_2

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def _serve():
        conn, _ = listener.accept()
        try:
            with server_ctx.wrap_socket(conn, server_side=True):
                pass
        except (ssl.SSLError, OSError):
            pass

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
            with pytest.raises(ssl.SSLCertVerificationError):
                client_ctx.wrap_socket(raw, server_hostname="api.stripe.com")
    finally:
        thread.join(timeout=5)
        listener.close()


# ---- P1 · the environment we publish -------------------------------------------------------
def test_proxy_env_carries_the_flags_that_matter(ca):
    env = lp.proxy_env(18791, "tok en/+", ca.bundle_path, "treg.to")
    assert env["HTTPS_PROXY"] == "http://treg:tok%20en%2F%2B@127.0.0.1:18791"
    assert env["https_proxy"] == env["HTTPS_PROXY"]        # curl reads lowercase
    assert env["NODE_USE_ENV_PROXY"] == "1"                # Node's fetch ignores the proxy without it
    assert env["NODE_EXTRA_CA_CERTS"] == str(ca.bundle_path)
    for key in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO",
                "DENO_CERT", "AWS_CA_BUNDLE"):
        assert env[key] == str(ca.bundle_path)
    # Loopback and the registry itself must never come back through us.
    assert "127.0.0.1" in env["NO_PROXY"] and "treg.to" in env["NO_PROXY"]
    assert env["no_proxy"] == env["NO_PROXY"]


def test_handle_env_matches_the_running_proxy(ca):
    handle = lp.start(lp.ProxyConfig(token=lp.mint_token(), port=0, ca=ca))
    try:
        env = handle.env("treg.example")
        assert f"@127.0.0.1:{handle.port}" in env["HTTPS_PROXY"]
        assert env["SSL_CERT_FILE"] == str(ca.bundle_path)
    finally:
        handle.stop()


def test_proxy_dir_follows_treg_config(tmp_path, monkeypatch):
    """Tests and per-agent identities redirect the CLI with TREG_CONFIG; the CA must follow, or a test
    run writes into the developer's real ~/.treg."""
    monkeypatch.setenv("TREG_CONFIG", str(tmp_path / "cfg" / "config.json"))
    assert lp.proxy_dir() == tmp_path / "cfg" / "proxy"
    monkeypatch.delenv("TREG_CONFIG")
    assert lp.proxy_dir().name == "proxy"


# ---- lifecycle ------------------------------------------------------------------------------
def test_stop_is_idempotent():
    handle = lp.start(lp.ProxyConfig(token=lp.mint_token(), port=0))
    handle.stop()
    handle.stop()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", handle.port), timeout=2).close()


def test_a_broken_connection_does_not_take_the_proxy_down(proxy):
    """One misbehaving client must never end the session for the rest of the agent's calls."""
    s = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
    s.sendall(b"CONNECT ")      # hang up mid-head
    s.close()
    echo = _EchoServer()
    try:
        good = _proxy_connect(proxy.port, proxy.token, f"127.0.0.1:{echo.port}")
        assert _read_head(good).startswith(b"HTTP/1.1 200 ")
        good.close()
    finally:
        echo.close()


def test_an_oversized_head_is_refused(proxy):
    """A client that never finishes its head must not be able to make us buffer without bound."""
    cred = base64.b64encode(f"treg:{proxy.token}".encode()).decode()
    s = socket.create_connection(("127.0.0.1", proxy.port), timeout=10)
    s.sendall(f"CONNECT example.com:443 HTTP/1.1\r\nProxy-Authorization: Basic {cred}\r\n".encode())
    filler = b"X-Pad: " + b"a" * 4000 + b"\r\n"
    try:
        for _ in range(40):                                # ~160 KB, past the 64 KB head cap
            s.sendall(filler)
    except OSError:
        pass                                               # the proxy may have closed us first
    head = _read_head(s)
    assert head == b"" or head.startswith(b"HTTP/1.1 431 ")
    s.close()


@pytest.mark.asyncio
async def test_handle_client_survives_an_unexpected_error(monkeypatch):
    """The catch-all around a connection: a bug in one path returns, it does not kill the listener."""
    monkeypatch.setattr(lp, "_parse_head", lambda *_: (_ for _ in ()).throw(RuntimeError("boom")))
    reader = asyncio.StreamReader()
    reader.feed_data(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
    reader.feed_eof()

    class _W:
        def write(self, _data): pass
        async def drain(self): pass
        def close(self): self.closed = True
        def can_write_eof(self): return False

    writer = _W()
    await lp.handle_client(lp.ProxyConfig(token="t"), 18791, reader, writer)   # must not raise
    assert getattr(writer, "closed", False)


# ---- P2 · interception: the call goes to treg, the key never comes here --------------------
class _FakeTreg:
    """Stands in for the registry. Records every request that lands on `/call/` and answers from a
    queue, so a test can assert exactly what the proxy sent — the byte-faithfulness check the plan
    asks for — without a server on a socket."""

    def __init__(self):
        self.seen: list[httpx.Request] = []
        self.replies: list = []

    def transport(self) -> httpx.MockTransport:
        async def _handle(request: httpx.Request) -> httpx.Response:
            await request.aread()
            self.seen.append(request)
            if not self.replies:
                return httpx.Response(200, json={"ok": True})
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        return httpx.MockTransport(_handle)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport(), trust_env=False)


@pytest.fixture()
def treg():
    return _FakeTreg()


@pytest.fixture()
def intercepting(ca, treg):
    """A proxy that intercepts api.stripe.com and nothing else."""
    cfg = lp.ProxyConfig(
        token=lp.mint_token(), port=0, ca=ca,
        base_url="https://treg.example", treg_token="member-token-abc", org="acme",
        client_name="claude-code", hosts=frozenset({"api.stripe.com"}),
    )
    handle = lp.start(cfg, client=treg.client())
    handle.cfg = cfg
    yield handle
    handle.stop()


def _trusting(ca) -> ssl.SSLContext:
    """What `SSL_CERT_FILE=<bundle>` gives a process: the system roots plus ours. The TLS floor is
    pinned rather than left to the default, so nothing here can quietly accept TLS 1.0/1.1."""
    ctx = ssl.create_default_context(cafile=str(ca.bundle_path))
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def _agent(handle, ca) -> httpx.Client:
    """An ordinary HTTP client set up exactly as the agent's process tree will be: pointed at the
    proxy, trusting our bundle. It has no idea treg exists."""
    return httpx.Client(
        proxy=f"http://treg:{handle.token}@127.0.0.1:{handle.port}",
        verify=_trusting(ca), timeout=10,
    )


def test_intercepted_call_is_re_addressed_to_treg(intercepting, ca, treg):
    """The heart of P2. The agent calls Stripe; the request lands on treg's `/call/` passthrough
    carrying the member's treg token — and no vendor key exists anywhere on this machine."""
    with _agent(intercepting, ca) as agent:
        r = agent.get("https://api.stripe.com/v1/charges?limit=3")
    assert r.status_code == 200 and r.json() == {"ok": True}

    sent = treg.seen[0]
    assert str(sent.url) == "https://treg.example/call/https://api.stripe.com/v1/charges?limit=3"
    assert sent.headers["x-treg-token"] == "member-token-abc"
    assert sent.headers["x-treg-org"] == "acme"
    assert sent.headers["x-treg-client"] == "claude-code"


def test_method_body_and_duplicate_headers_survive(intercepting, ca, treg):
    """Faithful relay: what the agent typed is what treg receives."""
    with _agent(intercepting, ca) as agent:
        r = agent.request(
            "POST", "https://api.stripe.com/v1/charges?tag=a&tag=b",
            content=b'{"amount": 100}',
            headers=[("Content-Type", "application/json"), ("Cookie", "a=1"), ("Cookie", "b=2")],
        )
    assert r.status_code == 200
    sent = treg.seen[0]
    assert sent.method == "POST"
    assert sent.content == b'{"amount": 100}'
    assert "tag=a&tag=b" in str(sent.url)          # duplicate query keys kept
    assert sent.headers.get_list("cookie") == ["a=1", "b=2"]


def test_the_caller_cannot_speak_as_someone_else(intercepting, ca, treg):
    """An agent that sets its own `X-Treg-Token` must not be able to spend another member's quota
    through our proxy. The proxy's identity is the only one that reaches treg."""
    with _agent(intercepting, ca) as agent:
        agent.get("https://api.stripe.com/v1/charges",
                  headers={"X-Treg-Token": "stolen", "X-Treg-Org": "victim", "X-Treg-Client": "liar"})
    sent = treg.seen[0]
    assert sent.headers["x-treg-token"] == "member-token-abc"
    assert sent.headers["x-treg-org"] == "acme"
    assert sent.headers["x-treg-client"] == "claude-code"
    assert "stolen" not in str(sent.headers)


def test_only_allow_listed_hosts_are_decrypted(intercepting, ca, treg):
    """Non-negotiable #3. A host that is not a registered tool is tunnelled: no certificate is signed
    for it, and treg never hears about it."""
    echo = _EchoServer()
    try:
        s = _proxy_connect(intercepting.port, intercepting.token, f"127.0.0.1:{echo.port}")
        assert _read_head(s).startswith(b"HTTP/1.1 200 ")
        s.sendall(b"secret bytes")
        assert s.recv(4096) == b"SECRET BYTES"
        s.close()
    finally:
        echo.close()
    assert "127.0.0.1" not in ca._contexts
    assert treg.seen == []


def test_a_response_streams_back_without_a_content_length(intercepting, ca, treg):
    """treg may answer with no length (a streamed upstream). We must re-frame it ourselves rather
    than copy framing that no longer applies to our hop."""
    async def _body():
        for part in (b"one ", b"two ", b"three"):
            yield part

    treg.replies.append(httpx.Response(200, content=_body(), headers={"Content-Type": "text/plain"}))
    with _agent(intercepting, ca) as agent:
        r = agent.get("https://api.stripe.com/v1/charges")
    assert r.status_code == 200 and r.content == b"one two three"


def test_upstream_status_and_headers_come_back_verbatim(intercepting, ca, treg):
    treg.replies.append(httpx.Response(402, json={"error": "card_declined"},
                                       headers={"X-Request-Id": "req_123"}))
    with _agent(intercepting, ca) as agent:
        r = agent.get("https://api.stripe.com/v1/charges")
    assert r.status_code == 402
    assert r.json() == {"error": "card_declined"}
    assert r.headers["x-request-id"] == "req_123"


def test_a_head_request_gets_no_body(intercepting, ca, treg):
    treg.replies.append(httpx.Response(200, headers={"Content-Length": "1234"}))
    with _agent(intercepting, ca) as agent:
        r = agent.head("https://api.stripe.com/v1/charges")
    assert r.status_code == 200 and r.content == b""


def test_two_calls_share_one_intercepted_connection(intercepting, ca, treg):
    """Keep-alive: an agent making several calls must not pay for a handshake each time."""
    with _agent(intercepting, ca) as agent:
        assert agent.get("https://api.stripe.com/v1/charges").status_code == 200
        assert agent.get("https://api.stripe.com/v1/customers").status_code == 200
    assert len(treg.seen) == 2
    assert str(treg.seen[1].url).endswith("/v1/customers")


def test_treg_being_down_does_not_look_like_the_vendor_being_down(intercepting, ca, treg):
    """An agent told '502 Bad Gateway' would blame Stripe and retry against a healthy service. The
    message has to name treg, and say the call was not made."""
    treg.replies.append(httpx.ConnectError("no route"))
    with _agent(intercepting, ca) as agent:
        r = agent.get("https://api.stripe.com/v1/charges")
    assert r.status_code == 502
    assert "cannot reach the registry" in r.text and "was NOT made" in r.text


def test_an_edge_waf_block_is_retried_base64(intercepting, ca, treg):
    """Render's edge 403s a body that looks like SQL injection. The CLI already re-sends such a body
    base64-encoded under X-Treg-Body-Encoding; an intercepted call has to do the same or a
    legitimate query fails for a reason no one can see."""
    treg.replies.append(httpx.Response(403, html="<html>blocked</html>"))
    treg.replies.append(httpx.Response(200, json={"ok": True}))
    with _agent(intercepting, ca) as agent:
        r = agent.post("https://api.stripe.com/v1/query", content=b"SELECT * FROM charges")
    assert r.status_code == 200
    assert len(treg.seen) == 2
    assert treg.seen[1].headers["x-treg-body-encoding"] == "base64"
    assert base64.b64decode(treg.seen[1].content) == b"SELECT * FROM charges"


def test_a_json_403_from_treg_is_not_retried(intercepting, ca, treg):
    """treg's own refusals are JSON. Retrying one base64-encoded would double every denied call."""
    treg.replies.append(httpx.Response(403, json={"detail": "you do not have access"}))
    with _agent(intercepting, ca) as agent:
        r = agent.post("https://api.stripe.com/v1/charges", content=b"x")
    assert r.status_code == 403 and len(treg.seen) == 1


def test_a_pinned_client_fails_alone(intercepting, ca, treg):
    """A client that trusts only the system roots refuses our certificate. That is a known limit —
    what matters is that it takes down nothing but itself."""
    with httpx.Client(proxy=f"http://treg:{intercepting.token}@127.0.0.1:{intercepting.port}",
                      timeout=10) as strict:
        with pytest.raises(httpx.ConnectError):
            strict.get("https://api.stripe.com/v1/charges")
    with _agent(intercepting, ca) as agent:            # the proxy is still healthy
        assert agent.get("https://api.stripe.com/v1/charges").status_code == 200


def test_interception_needs_a_credential(ca):
    """A proxy with no treg identity must not decrypt anything: it would terminate TLS and then have
    no way to complete the call, breaking the agent for an invisible reason."""
    base = dict(token="t", ca=ca, hosts=frozenset({"api.stripe.com"}))
    assert not lp.ProxyConfig(**base).intercepts("api.stripe.com")                       # no base_url
    assert not lp.ProxyConfig(**base, base_url="https://x").intercepts("api.stripe.com")  # no token
    full = lp.ProxyConfig(**base, base_url="https://x", treg_token="tok")
    assert full.intercepts("api.stripe.com") and full.intercepts("API.STRIPE.COM")
    assert not full.intercepts("api.openai.com")
    assert not lp.ProxyConfig(token="t", base_url="https://x", treg_token="tok",
                              hosts=frozenset({"api.stripe.com"})).intercepts("api.stripe.com")  # no CA


# ---- P2 · unit pieces ----------------------------------------------------------------------
def test_treg_url_uses_the_passthrough_shape():
    assert lp._treg_url("https://treg.example", "api.stripe.com", "/v1/charges?a=1") == \
        "https://treg.example/call/https://api.stripe.com/v1/charges?a=1"
    assert lp._treg_url("https://treg.example/", "api.stripe.com", "/") == \
        "https://treg.example/call/https://api.stripe.com/"


def test_forward_headers_drops_transport_and_adds_control():
    cfg = lp.ProxyConfig(token="t", base_url="https://x", treg_token="tok", org="acme")
    out = lp._forward_headers(cfg, [("Host", "api.stripe.com"), ("Connection", "keep-alive"),
                                    ("Content-Length", "3"), ("Accept", "application/json")])
    names = {k.lower() for k, _ in out}
    assert "host" not in names and "connection" not in names and "content-length" not in names
    assert "accept" in names
    # No Accept-Encoding from the caller → ask for identity, or httpx requests gzip on its own and
    # the agent gets compressed bytes it never asked for.
    assert ("Accept-Encoding", "identity") in out


def test_forward_headers_keeps_the_callers_encoding_choice():
    cfg = lp.ProxyConfig(token="t", base_url="https://x", treg_token="tok")
    out = lp._forward_headers(cfg, [("Accept-Encoding", "gzip")])
    assert [v for k, v in out if k.lower() == "accept-encoding"] == ["gzip"]


def test_header_pairs_keeps_duplicates_and_spelling():
    head = b"GET / HTTP/1.1\r\nCookie: a=1\r\nCookie: b=2\r\nX-Odd-Case: v"
    assert lp._header_pairs(head) == [("Cookie", "a=1"), ("Cookie", "b=2"), ("X-Odd-Case", "v")]


def test_waf_detection_only_fires_on_an_html_403():
    assert lp._is_waf_block(403, "text/html; charset=utf-8")
    assert not lp._is_waf_block(403, "application/json")
    assert not lp._is_waf_block(500, "text/html")


def test_the_proxys_own_client_ignores_https_proxy(monkeypatch):
    """Non-negotiable #6. httpx reads HTTPS_PROXY from its own environment — inside a treg shell that
    points at THIS proxy, so the first intercepted call would come straight back to us and loop."""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:18791")
    client = lp.treg_client()
    assert client.trust_env is False
    assert client._mounts == {}          # no proxy transport was mounted from the environment


# ---- P2 · end to end, against the real registry ---------------------------------------------
async def test_the_agent_calls_the_vendor_and_treg_injects_the_key(clients, ca):
    """The proof P2 exists for, run against the actual FastAPI app rather than a stand-in.

    A tool is registered with a secret. An ordinary HTTP client — no treg awareness, no key —
    calls the vendor URL through the proxy. The credential appears at the upstream because the
    SERVER put it there; the agent never held it, and neither did the proxy.
    """
    from httpx import ASGITransport, AsyncClient

    from treg import api

    sid = (await clients.post("/secrets", json={"name": "intercom-k", "value": "SEK-live-xyz"})).json()["id"]
    r = await clients.post("/tools", json={"name": "intercom", "base_url": "https://api.intercom.io",
                                           "secret_id": sid})
    assert r.status_code == 200, r.text

    cfg = lp.ProxyConfig(
        token=lp.mint_token(), port=0, ca=ca,
        base_url="http://registry", treg_token=clients.headers["X-Treg-Token"],
        client_name="claude-code", hosts=frozenset({"api.intercom.io"}),
    )
    registry = AsyncClient(transport=ASGITransport(app=api.app), base_url="http://registry")
    server = await lp.serve(cfg, client=registry)
    port = lp.listening_port(server, 0)

    def _agent_call():
        # Deliberately a plain client with no idea treg exists — only the two things the shell's
        # environment gives it: the proxy address and the trust bundle.
        with httpx.Client(proxy=f"http://treg:{cfg.token}@127.0.0.1:{port}",
                          verify=_trusting(ca), timeout=10) as agent:
            return agent.get("https://api.intercom.io/echo?per_page=5",
                             headers={"X-Custom": "from-the-agent"})

    try:
        resp = await asyncio.to_thread(_agent_call)
    finally:
        server.close()
        await server.wait_closed()
        await registry.aclose()

    assert resp.status_code == 200, resp.text
    echoed = resp.json()
    assert echoed["auth"] == "Bearer SEK-live-xyz"      # injected server-side, by the existing relay
    assert echoed["query"]["per_page"] == "5"           # the agent's own query survived the trip
    assert echoed["headers"]["x-custom"] == "from-the-agent"
    # treg's control headers are the proxy's, and the relay strips them before the vendor sees them.
    assert "x-treg-token" not in echoed["headers"]
    # Nothing the agent's process could read ever contained the key.
    assert "SEK-live-xyz" not in str(cfg) and "SEK-live-xyz" not in str(cfg.__dict__)
    assert "SEK-live-xyz" not in ca.bundle_path.read_text()


async def test_an_unregistered_host_is_a_readable_404(clients, ca):
    """P3 refines the wording; what matters already is that the answer comes from treg and says
    something true, rather than the agent seeing a broken connection."""
    from httpx import ASGITransport, AsyncClient

    from treg import api

    cfg = lp.ProxyConfig(
        token=lp.mint_token(), port=0, ca=ca, base_url="http://registry",
        treg_token=clients.headers["X-Treg-Token"], hosts=frozenset({"api.nothing-here.com"}),
    )
    registry = AsyncClient(transport=ASGITransport(app=api.app), base_url="http://registry")
    server = await lp.serve(cfg, client=registry)
    port = lp.listening_port(server, 0)

    def _agent_call():
        with httpx.Client(proxy=f"http://treg:{cfg.token}@127.0.0.1:{port}",
                          verify=_trusting(ca), timeout=10) as agent:
            return agent.get("https://api.nothing-here.com/v1/thing")

    try:
        resp = await asyncio.to_thread(_agent_call)
    finally:
        server.close()
        await server.wait_closed()
        await registry.aclose()

    assert resp.status_code == 404
    assert resp.json()["host"] == "api.nothing-here.com"


# ---- P3 · the allow-list and readable errors ------------------------------------------------
async def test_the_allow_list_comes_from_the_members_own_tool_list(clients, ca):
    """`GET /tools` is already filtered to what THIS member may use, so the allow-list inherits the
    per-member tool list and project scope for free — a host they cannot use is never decrypted."""
    from httpx import ASGITransport, AsyncClient

    from treg import api

    for name, base in (("intercom", "https://api.intercom.io"), ("stripe", "https://api.stripe.com")):
        sid = (await clients.post("/secrets", json={"name": f"{name}-k", "value": "x"})).json()["id"]
        await clients.post("/tools", json={"name": name, "base_url": base, "secret_id": sid})

    registry = AsyncClient(transport=ASGITransport(app=api.app), base_url="http://registry")
    try:
        hosts = await lp.fetch_hosts(registry, "http://registry", clients.headers["X-Treg-Token"])
    finally:
        await registry.aclose()
    assert hosts == frozenset({"api.intercom.io", "api.stripe.com"})


async def test_a_failed_fetch_intercepts_nothing(clients):
    """No answer must never mean 'intercept everything'."""
    from httpx import ASGITransport, AsyncClient

    from treg import api

    registry = AsyncClient(transport=ASGITransport(app=api.app), base_url="http://registry")
    try:
        assert await lp.fetch_hosts(registry, "http://registry", "not-a-real-token") == frozenset()
    finally:
        await registry.aclose()

    async def _boom(request):
        raise httpx.ConnectError("down")

    dead = httpx.AsyncClient(transport=httpx.MockTransport(_boom), trust_env=False)
    try:
        assert await lp.fetch_hosts(dead, "http://registry", "tok") == frozenset()
    finally:
        await dead.aclose()


async def test_refresh_picks_up_a_tool_registered_mid_session(treg):
    """A tool added ten minutes into a shell session should start working without a restart."""
    cfg = lp.ProxyConfig(token="t", base_url="http://registry", treg_token="tok")
    treg.replies.append(httpx.Response(200, json=[{"host": "api.stripe.com"}, {"host": "API.NEW.COM"}]))
    client = treg.client()
    try:
        task = asyncio.create_task(lp.refresh_hosts_forever(cfg, client, interval=0))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if cfg.hosts:
                break
        task.cancel()
    finally:
        await client.aclose()
    assert cfg.hosts == frozenset({"api.stripe.com", "api.new.com"})   # lowercased for matching


async def test_a_transient_failure_never_empties_a_working_allow_list(treg):
    """Applying an empty answer would silently stop injecting for five minutes, and the agent's calls
    would go out with no credential."""
    cfg = lp.ProxyConfig(token="t", base_url="http://registry", treg_token="tok",
                         hosts=frozenset({"api.stripe.com"}))
    treg.replies.append(httpx.Response(500))
    client = treg.client()
    try:
        task = asyncio.create_task(lp.refresh_hosts_forever(cfg, client, interval=0))
        await asyncio.sleep(0.05)
        task.cancel()
    finally:
        await client.aclose()
    assert cfg.hosts == frozenset({"api.stripe.com"})


def test_treg_errors_say_who_refused_and_what_fixes_it():
    """A raw 404 reads as 'the vendor has no such endpoint', and an agent 'fixes' it by rewriting a
    perfectly good URL. Every message names treg and the next action."""
    m404 = lp.treg_error_message(404, "api.stripe.com", "no registered tool")
    assert "treg" in m404 and "treg tools add" in m404
    assert {"api.stripe.com"} <= set(m404.split())  # the whole host, not a substring of a longer one
    assert "not api.stripe.com's" in m404          # says whose answer this is NOT
    assert "ask an admin" in lp.treg_error_message(403, "api.stripe.com", "").lower()
    assert "treg login" in lp.treg_error_message(401, "api.stripe.com", "")
    assert "daily call limit" in lp.treg_error_message(429, "api.stripe.com", "")
    assert "more than one" in lp.treg_error_message(409, "api.stripe.com", "")
    assert "418" in lp.treg_error_message(418, "api.stripe.com", "teapot")


async def test_an_unregistered_host_is_explained_not_just_404d(clients, ca):
    """End to end: treg marks its own refusal, the proxy turns it into an instruction."""
    from httpx import ASGITransport, AsyncClient

    from treg import api

    cfg = lp.ProxyConfig(
        token=lp.mint_token(), port=0, ca=ca, base_url="http://registry",
        treg_token=clients.headers["X-Treg-Token"], hosts=frozenset({"api.unregistered.com"}),
    )
    registry = AsyncClient(transport=ASGITransport(app=api.app), base_url="http://registry")
    server = await lp.serve(cfg, client=registry)
    port = lp.listening_port(server, 0)

    def _agent_call():
        with httpx.Client(proxy=f"http://treg:{cfg.token}@127.0.0.1:{port}",
                          verify=_trusting(ca), timeout=10) as agent:
            return agent.get("https://api.unregistered.com/v1/thing")

    try:
        resp = await asyncio.to_thread(_agent_call)
    finally:
        server.close()
        await server.wait_closed()
        await registry.aclose()

    assert resp.status_code == 404
    payload = resp.json()
    assert payload["source"] == "treg"
    assert "no tool is registered for api.unregistered.com" in payload["error"]
    assert "treg tools add" in payload["error"]


def test_a_vendors_own_error_is_never_rewritten(intercepting, ca, treg):
    """The mapping fires only on treg's `X-Treg-Error` marker. A genuine 404 from the vendor, relayed
    through treg, must reach the agent exactly as the vendor wrote it."""
    treg.replies.append(httpx.Response(404, json={"error": {"message": "No such charge: ch_123"}}))
    with _agent(intercepting, ca) as agent:
        r = agent.get("https://api.stripe.com/v1/charges/ch_123")
    assert r.status_code == 404
    assert r.json() == {"error": {"message": "No such charge: ch_123"}}
    assert "treg" not in r.text


def test_an_older_registry_without_the_marker_passes_through(intercepting, ca, treg):
    """Against a registry that predates the marker nothing fires, and the reply is untouched."""
    treg.replies.append(httpx.Response(403, json={"detail": "tool not in your access list"}))
    with _agent(intercepting, ca) as agent:
        r = agent.get("https://api.stripe.com/v1/charges")
    assert r.status_code == 403 and r.json() == {"detail": "tool not in your access list"}


def test_the_explained_error_keeps_the_connection_usable(intercepting, ca, treg):
    """An explained refusal is still an ordinary HTTP response — the next call on the same connection
    has to work."""
    treg.replies.append(httpx.Response(403, json={"detail": "nope"}, headers={"X-Treg-Error": "1"}))
    with _agent(intercepting, ca) as agent:
        first = agent.get("https://api.stripe.com/v1/charges")
        second = agent.get("https://api.stripe.com/v1/customers")
    assert first.status_code == 403 and first.json()["source"] == "treg"
    assert second.status_code == 200


# ---- the daemon's state file (`treg serve`) --------------------------------------------------
def test_state_file_is_owner_only(tmp_path, monkeypatch):
    """`treg shell --proxy` keeps its token in the subshell's environment; a DAEMON has to write it
    down so another terminal can find it. That file is the whole extra risk of `treg serve`."""
    monkeypatch.setenv("TREG_CONFIG", str(tmp_path / "config.json"))
    path = lp.write_state(18791, "tok", os.getpid(), "https://treg.example", "acme", ["b.com", "a.com"])
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    state = lp.read_state()
    assert state["port"] == 18791 and state["token"] == "tok"
    assert state["hosts"] == ["a.com", "b.com"]               # sorted, so `status` reads stably


def test_a_dead_daemon_does_not_look_alive(tmp_path, monkeypatch):
    """A state file left behind by a killed process would otherwise make `status` insist forever that
    a proxy is running, and `start` refuse to replace it."""
    monkeypatch.setenv("TREG_CONFIG", str(tmp_path / "config.json"))
    lp.write_state(18791, "tok", 999_999, "https://treg.example", "", [])
    assert lp.running() is None
    assert not lp.state_path().exists()                        # and the stale file is cleaned up


def test_a_live_daemon_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("TREG_CONFIG", str(tmp_path / "config.json"))
    lp.write_state(18791, "tok", os.getpid(), "https://treg.example", "", ["a.com"])
    assert lp.running()["port"] == 18791
    lp.clear_state()
    assert lp.running() is None


def test_a_missing_or_corrupt_state_file_is_just_not_running(tmp_path, monkeypatch):
    monkeypatch.setenv("TREG_CONFIG", str(tmp_path / "config.json"))
    assert lp.read_state() is None
    lp.state_path().parent.mkdir(parents=True, exist_ok=True)
    lp.state_path().write_text("{ not json")
    assert lp.read_state() is None and lp.running() is None


def test_pid_alive_is_honest():
    assert lp.pid_alive(os.getpid())
    assert not lp.pid_alive(999_999)
    assert not lp.pid_alive(0)
