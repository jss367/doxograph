"""Image destinations and renderer failures are exercised without networking."""
import io
import socket
from unittest.mock import Mock

import httpx
import pytest
from PIL import Image
from pypdf import PdfReader

from doxograph import posts

PUBLIC = "93.184.216.34"


def dns(monkeypatch, addresses):
    calls = []

    def resolve(host, port, **kwargs):
        calls.append((host, port))
        values = addresses[host] if isinstance(addresses, dict) else addresses
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET,
                 socket.SOCK_STREAM, 6, "", (ip, port)) for ip in values]

    monkeypatch.setattr(posts.socket, "getaddrinfo", resolve)
    return calls


@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.0.1", "169.254.169.254",
    "0.0.0.0", "100.64.0.1", "224.0.0.1", "::1", "fc00::1", "fe80::1",
    "ff02::1", "::ffff:127.0.0.1", "2002:7f00:1::1",
])
def test_nonpublic_dns_answer_never_reaches_http(monkeypatch, address):
    dns(monkeypatch, [address])
    calls = []
    with httpx.Client(transport=httpx.MockTransport(lambda r: calls.append(r))) as client:
        assert posts._image("https://images.example/figure.png", client) is None
    assert calls == []


@pytest.mark.parametrize("url", [
    "http://example.com:bad/figure.png", "http://[broken/figure.png",
    "file:///figure.png", "https:///figure.png", "http://localhost/figure.png",
    "http://host.local/figure.png", "http://host.localhost/figure.png",
    "http://[fe80::1%25en0]/figure.png", "http://user:password@example.com/figure.png",
])
def test_invalid_urls_never_resolve_or_request(monkeypatch, url):
    resolve = Mock(side_effect=AssertionError("unexpected DNS"))
    monkeypatch.setattr(posts.socket, "getaddrinfo", resolve)
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("unexpected HTTP"))) as client:
        assert posts._image(url, client) is None
    resolve.assert_not_called()


def test_mixed_public_private_answers_are_rejected(monkeypatch):
    dns(monkeypatch, [PUBLIC, "::1"])
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("unexpected HTTP"))) as client:
        assert posts._image("https://images.example/a", client) is None


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_to_private_destination_is_not_requested(monkeypatch, status):
    dns(monkeypatch, {"images.example": [PUBLIC], "internal.example": ["10.0.0.1"]})
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, headers={"location": "http://internal.example/a"})

    with httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True) as client:
        assert posts._image("https://images.example/a", client) is None
    assert len(calls) == 1
    assert calls[0].url.host == PUBLIC


@pytest.mark.parametrize("location", ["/next.png", "https://cdn.example/next.png"])
def test_public_redirects_preserve_host_tls_name_and_pin_address(monkeypatch, location):
    resolutions = dns(monkeypatch, [PUBLIC])
    calls = []

    def handle(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(302, headers={"location": location})
        return httpx.Response(200, content=b"image")

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert posts._image("https://images.example/start.png", client) == b"image"
    host = "cdn.example" if location.startswith("https") else "images.example"
    assert resolutions == [("images.example", 443), (host, 443)]
    assert all(r.url.host == PUBLIC for r in calls)
    assert calls[1].headers["host"] == host
    assert calls[1].extensions["sni_hostname"] == host
    assert calls[1].url.path == "/next.png"


def test_dns_is_not_reused_to_connect_after_validation(monkeypatch):
    resolve = Mock(side_effect=[[(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, 443))],
                               [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]])
    monkeypatch.setattr(posts.socket, "getaddrinfo", resolve)
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, content=b"image")

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert posts._image("https://images.example/a", client) == b"image"
    assert calls[0].url.host == PUBLIC
    assert resolve.call_count == 1


def test_redirect_loop_is_bounded(monkeypatch):
    dns(monkeypatch, [PUBLIC])
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "/loop"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert posts._image("https://images.example/a", client) is None
    assert len(calls) == 6


@pytest.mark.parametrize("response", [httpx.Response(404), httpx.Response(302),
                                        httpx.Response(200, content=b"12345")])
def test_http_failure_missing_location_and_size_limit(monkeypatch, response):
    dns(monkeypatch, [PUBLIC])
    monkeypatch.setattr(posts, "IMAGE_BYTES", 4)
    with httpx.Client(transport=httpx.MockTransport(lambda r: response)) as client:
        assert posts._image("https://images.example/a", client) is None


def test_dns_failure_leaves_placeholder(monkeypatch):
    monkeypatch.setattr(posts.socket, "getaddrinfo", Mock(side_effect=socket.gaierror("not found")))
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("unexpected HTTP"))) as client:
        assert posts._image("https://images.example/a", client) is None


def test_renderer_exception_keeps_pdf_and_image_placeholder(monkeypatch):
    data = io.BytesIO()
    Image.new("RGB", (10, 10), "navy").save(data, "PNG")
    monkeypatch.setattr(posts, "_image", lambda *a, **kwargs: data.getvalue())
    monkeypatch.setattr(posts.FPDF, "image", Mock(side_effect=RuntimeError("cannot place image")))
    meta = {"title": "Test", "authors": [], "year": 2026, "venue": "LessWrong",
            "source": {"url": "https://www.lesswrong.com/posts/example"}}
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("unexpected HTTP"))) as client:
        rendered = posts.render(meta, '<p>Before</p><img src="https://images.example/a" alt="figure"><p>After</p>', client)
    text = " ".join(page.extract_text() for page in PdfReader(io.BytesIO(rendered)).pages)
    assert all(value in text for value in ["Before", "[image: figure]", "After"])


@pytest.mark.parametrize("size", [(0, 10), (10, 0)])
def test_zero_dimensions_do_not_reach_renderer(monkeypatch, size):
    image = Mock()
    image.size = size
    opened = Mock()
    opened.__enter__ = Mock(return_value=image)
    opened.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(posts.Image, "open", Mock(return_value=opened))
    pdf = Mock()
    assert not posts._placed(pdf, b"mock", 100)
    pdf.image.assert_not_called()


@pytest.mark.parametrize("location", ["http://example.com:bad/a", "file:///a", "http://localhost/a"])
def test_bad_redirect_target_is_never_requested(monkeypatch, location):
    dns(monkeypatch, [PUBLIC])
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": location})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert posts._image("https://images.example/a", client) is None
    assert len(calls) == 1


def test_public_ipv6_and_explicit_port(monkeypatch):
    address = "2606:4700:4700::1111"
    resolutions = dns(monkeypatch, [address])
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, content=b"image")

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert posts._image("https://[2606:4700:4700::1111]:8443/a", client) == b"image"
    assert resolutions == [(address, 8443)]
    assert calls[0].headers["host"] == "[2606:4700:4700::1111]:8443"


def test_actual_transport_connects_to_pinned_ip_with_original_tls_name(monkeypatch):
    from httpcore._backends.sync import SyncBackend

    dns(monkeypatch, [PUBLIC])
    connections, tls, writes = [], [], []

    class Stream:
        response = b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nimage"

        def read(self, max_bytes, timeout=None):
            value, self.response = self.response, b""
            return value

        def write(self, data, timeout=None):
            writes.append(data)

        def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            tls.append(server_hostname)
            return self

        def get_extra_info(self, name):
            return None

        def close(self):
            pass

    def connect(self, host, port, **kwargs):
        connections.append((host, port))
        return Stream()

    monkeypatch.setattr(SyncBackend, "connect_tcp", connect)
    with httpx.Client(trust_env=False) as client:
        assert posts._image("https://images.example/a", client) == b"image"
    assert connections == [(PUBLIC, 443)]
    assert tls == ["images.example"]
    assert b"Host: images.example\r\n" in b"".join(writes)


def test_pixel_limit_precedes_decode_and_draw(monkeypatch):
    image = Mock()
    image.size = (4001, 4000)
    opened = Mock()
    opened.__enter__ = Mock(return_value=image)
    opened.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(posts.Image, "open", Mock(return_value=opened))
    pdf = Mock()
    assert not posts._placed(pdf, b"mock", 100)
    image.load.assert_not_called()
    pdf.image.assert_not_called()


@pytest.mark.parametrize("scheme,certificate_failure", [
    ("http", None), ("http", "image"),
    ("https", None), ("https", "proxy"), ("https", "image"),
])
@pytest.mark.parametrize("configuration", ["explicit", "environment"])
def test_https_proxy_keeps_pinned_destination_and_certificate_errors_fail_closed(
        monkeypatch, scheme, certificate_failure, configuration):
    """Real transport with inert socket fixtures: verify both TLS layers."""
    import ssl
    from httpcore._backends.sync import SyncBackend

    dns(monkeypatch, [PUBLIC])
    connections, tls, writes = [], [], []

    class ProxyStream:
        response = b"HTTP/1.1 200 Connection established\r\n\r\n"

        def read(self, max_bytes, timeout=None):
            value, self.response = self.response, b""
            return value

        def write(self, data, timeout=None):
            writes.append(data)

        def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            layer = "proxy" if scheme == "https" and not tls else "image"
            expected = "proxy.example" if layer == "proxy" else "images.example"
            assert ssl_context.verify_mode == ssl.CERT_REQUIRED
            assert ssl_context.check_hostname
            assert server_hostname == expected
            tls.append(server_hostname)
            if certificate_failure == layer:
                raise ssl.SSLCertVerificationError("mock invalid certificate")
            if layer == "image":
                self.response = b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nimage"
            return self

        def get_extra_info(self, name):
            return None

        def close(self):
            pass

    def connect(self, host, port, **kwargs):
        connections.append((host, port))
        return ProxyStream()

    monkeypatch.setattr(SyncBackend, "connect_tcp", connect)
    proxy = f"{scheme}://proxy.example:8080"
    if configuration == "environment":
        monkeypatch.setenv("HTTPS_PROXY", proxy)
        monkeypatch.setenv("NO_PROXY", "")
        options = {}
    else:
        options = {"proxy": proxy, "trust_env": False}
    with httpx.Client(**options) as client:
        data = posts._image("https://images.example/a", client)
    assert connections == [("proxy.example", 8080)]
    if certificate_failure == "proxy":
        assert tls == ["proxy.example"]
        assert not writes
    else:
        assert tls == (["proxy.example"] if scheme == "https" else []) + ["images.example"]
        assert b"CONNECT " + PUBLIC.encode() + b":443 HTTP/1.1" in b"".join(writes)
    if certificate_failure:
        assert data is None
        assert b"GET " not in b"".join(writes)
    else:
        assert data == b"image"
        assert b"Host: images.example\r\n" in b"".join(writes)


@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ConnectTimeout])
def test_unavailable_public_address_tries_next_pinned_address(monkeypatch, failure):
    ipv6 = "2606:4700:4700::1111"
    resolutions = dns(monkeypatch, [ipv6, PUBLIC, PUBLIC])
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.host == ipv6:
            raise failure("mock unreachable IPv6", request=request)
        return httpx.Response(200, content=b"image")

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert posts._image("https://images.example/a", client) == b"image"
    assert resolutions == [("images.example", 443)]
    assert [r.url.host for r in calls] == [ipv6, PUBLIC]
    assert all(r.headers["host"] == "images.example" for r in calls)
    assert all(r.extensions["sni_hostname"] == "images.example" for r in calls)


def test_every_address_must_be_public_before_fallback_attempts(monkeypatch):
    dns(monkeypatch, ["2606:4700:4700::1111", PUBLIC, "10.0.0.1"])
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("unexpected HTTP"))) as client:
        assert posts._image("https://images.example/a", client) is None


def test_all_public_addresses_fail_and_duplicates_are_not_retried(monkeypatch):
    dns(monkeypatch, ["2606:4700:4700::1111", PUBLIC, PUBLIC])
    calls = []

    def handle(request):
        calls.append(request.url.host)
        raise httpx.ConnectError("mock unreachable", request=request)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert posts._image("https://images.example/a", client) is None
    assert calls == ["2606:4700:4700::1111", PUBLIC]


def test_redirect_after_address_fallback_validates_new_destination(monkeypatch):
    ipv6 = "2606:4700:4700::1111"
    resolutions = dns(monkeypatch, {"images.example": [ipv6, PUBLIC], "internal.example": ["127.0.0.1"]})
    calls = []

    def handle(request):
        calls.append(request.url.host)
        if request.url.host == ipv6:
            raise httpx.ConnectError("mock unreachable", request=request)
        return httpx.Response(302, headers={"location": "http://internal.example/a"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert posts._image("https://images.example/a", client) is None
    assert calls == [ipv6, PUBLIC]
    assert resolutions == [("images.example", 443), ("internal.example", 80)]


def test_slow_drip_aborts_on_total_deadline_and_closes_stream(monkeypatch):
    dns(monkeypatch, [PUBLIC])
    now = [0.0]
    monkeypatch.setattr(posts, "monotonic", lambda: now[0])
    monkeypatch.setattr(posts, "IMAGE_SECONDS", 5.0)

    class Drip(httpx.SyncByteStream):
        count, closed = 0, False

        def __iter__(self):
            for _ in range(100):
                now[0] += 2.0
                self.count += 1
                yield b"x"

        def close(self):
            self.closed = True

    stream = Drip()
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=stream))) as client:
        assert posts._image("https://images.example/a", client) is None
    assert stream.count == 3
    assert stream.closed


def test_compressed_stream_checks_deadline_before_decoder_emits_data(monkeypatch):
    import gzip

    dns(monkeypatch, [PUBLIC])
    now = [0.0]
    monkeypatch.setattr(posts, "monotonic", lambda: now[0])
    monkeypatch.setattr(posts, "IMAGE_SECONDS", 5.0)

    class CompressedDrip(httpx.SyncByteStream):
        count, closed = 0, False

        def __iter__(self):
            # The gzip header produces no decoded bytes in the first chunks.
            for value in gzip.compress(b"image"):
                now[0] += 2.0
                self.count += 1
                yield bytes([value])

        def close(self):
            self.closed = True

    stream = CompressedDrip()
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(
            200, headers={"content-encoding": "gzip"}, stream=stream))) as client:
        assert posts._image("https://images.example/a", client) is None
    assert stream.count == 3
    assert stream.closed


def test_redirects_share_deadline_and_shrink_request_timeouts(monkeypatch):
    dns(monkeypatch, [PUBLIC])
    now = [0.0]
    monkeypatch.setattr(posts, "monotonic", lambda: now[0])
    monkeypatch.setattr(posts, "IMAGE_SECONDS", 5.0)
    calls = []

    def handle(request):
        calls.append(request)
        now[0] += 2.0
        return httpx.Response(302, headers={"location": "/next"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert posts._image("https://images.example/a", client) is None
    assert len(calls) == 3
    assert [r.extensions["timeout"]["read"] for r in calls] == [5.0, 3.0, 1.0]
    assert [r.extensions["timeout"]["connect"] for r in calls] == [5.0, 3.0, 1.0]


def test_address_retries_share_deadline(monkeypatch):
    dns(monkeypatch, ["2606:4700:4700::1111", PUBLIC, "1.1.1.1"])
    now = [0.0]
    monkeypatch.setattr(posts, "monotonic", lambda: now[0])
    monkeypatch.setattr(posts, "IMAGE_SECONDS", 5.0)
    calls = []

    def handle(request):
        calls.append(request)
        now[0] += 3.0
        raise httpx.ConnectTimeout("mock timeout", request=request)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert posts._image("https://images.example/a", client) is None
    assert len(calls) == 2
    assert [r.extensions["timeout"]["connect"] for r in calls] == [5.0, 2.0]


def test_dns_elapsed_time_can_exhaust_budget_before_any_request(monkeypatch):
    now = [0.0]
    resolve = dns(monkeypatch, [PUBLIC])
    original = posts.socket.getaddrinfo
    monkeypatch.setattr(posts, "monotonic", lambda: now[0])
    monkeypatch.setattr(posts, "IMAGE_SECONDS", 5.0)

    def slow_dns(*args, **kwargs):
        now[0] = 6.0
        return original(*args, **kwargs)

    monkeypatch.setattr(posts.socket, "getaddrinfo", slow_dns)
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("unexpected HTTP"))) as client:
        assert posts._image("https://images.example/a", client) is None
    assert resolve == [("images.example", 443)]


def test_read_failure_does_not_retry_another_address(monkeypatch):
    dns(monkeypatch, [PUBLIC, "1.1.1.1"])
    calls = []

    class Broken(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            yield b"partial"
            raise httpx.ReadTimeout("mock stalled body")

        def close(self):
            self.closed = True

    stream = Broken()

    def handle(request):
        calls.append(request)
        return httpx.Response(200, stream=stream)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert posts._image("https://images.example/a", client) is None
    assert len(calls) == 1
    assert stream.closed


def test_compressed_image_finishing_within_budget_is_preserved(monkeypatch):
    import gzip

    dns(monkeypatch, [PUBLIC])
    now = [0.0]
    monkeypatch.setattr(posts, "monotonic", lambda: now[0])
    monkeypatch.setattr(posts, "IMAGE_SECONDS", 5.0)

    class Chunks(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            now[0] += 1.0
            yield gzip.compress(b"image")

        def close(self):
            self.closed = True

    stream = Chunks()
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(
            200, headers={"content-encoding": "gzip"}, stream=stream))) as client:
        assert posts._image("https://images.example/a", client) == b"image"
    assert stream.closed


def test_headers_finishing_after_budget_skip_body_and_close_response(monkeypatch):
    dns(monkeypatch, [PUBLIC])
    now = [0.0]
    monkeypatch.setattr(posts, "monotonic", lambda: now[0])
    monkeypatch.setattr(posts, "IMAGE_SECONDS", 5.0)

    class Body(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            pytest.fail("body read after deadline")
            yield b"image"

        def close(self):
            self.closed = True

    stream = Body()

    def handle(request):
        now[0] = 6.0
        return httpx.Response(200, stream=stream)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert posts._image("https://images.example/a", client) is None
    assert stream.closed


def header_socket(monkeypatch, now, chunks, delay):
    """Fake the actual httpcore socket boundary; never open a socket."""
    import httpcore
    from httpcore._backends.sync import SyncBackend

    state = {"reads": [], "writes": [], "tls": [], "connections": [], "closed": False}
    chunks = iter(chunks)

    class Stream:
        def read(self, max_bytes, timeout=None):
            state["reads"].append(timeout)
            if timeout is not None and timeout < delay:
                now[0] += timeout
                raise httpcore.ReadTimeout("mock socket deadline")
            now[0] += delay
            return next(chunks, b"")

        def write(self, data, timeout=None):
            state["writes"].append((data, timeout))

        def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            state["tls"].append((server_hostname, timeout))
            # Return a new stream just as the real TLS backend does, so its
            # reads must receive their own guard after the handshake.
            return Stream()

        def get_extra_info(self, name):
            return None

        def close(self):
            state["closed"] = True

    def connect(self, host, port, **kwargs):
        state["connections"].append((host, port, kwargs["timeout"]))
        return Stream()

    monkeypatch.setattr(SyncBackend, "connect_tcp", connect)
    return state


@pytest.mark.parametrize("mode", ["http", "https", "proxy_connect"])
def test_socket_deadline_covers_slow_drip_headers_before_response_exists(monkeypatch, mode):
    dns(monkeypatch, [PUBLIC])
    now = [0.0]
    monkeypatch.setattr(posts, "monotonic", lambda: now[0])
    monkeypatch.setattr(posts, "IMAGE_SECONDS", 5.0)
    # Each byte is quicker than the original per-read timeout, but the header
    # parser cannot finish within the shared budget.
    state = header_socket(monkeypatch, now, [bytes([c]) for c in b"HTTP/1.1 200 OK\r\n\r\n"], 2.0)
    kwargs = {"trust_env": False}
    if mode == "proxy_connect":
        kwargs["proxy"] = "http://proxy.example:8080"
    scheme = "http" if mode == "http" else "https"
    with httpx.Client(**kwargs) as client:
        assert posts._image(f"{scheme}://images.example/a", client) is None
    assert now[0] == 5.0
    assert state["reads"] == [5.0, 3.0, 1.0]
    assert state["closed"]
    if mode == "proxy_connect":
        assert state["connections"] == [("proxy.example", 8080, 5.0)]
        assert state["tls"] == []  # The CONNECT headers never completed.
        assert b"CONNECT " + PUBLIC.encode() + b":443" in b"".join(d for d, _ in state["writes"])
    else:
        assert state["connections"] == [(PUBLIC, 80 if mode == "http" else 443, 5.0)]
        if mode == "https":
            assert state["tls"] == [("images.example", 5.0)]


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_socket_headers_and_body_finishing_within_budget_are_preserved(monkeypatch, scheme):
    dns(monkeypatch, [PUBLIC])
    now = [0.0]
    monkeypatch.setattr(posts, "monotonic", lambda: now[0])
    monkeypatch.setattr(posts, "IMAGE_SECONDS", 5.0)
    state = header_socket(monkeypatch, now, [b"HTTP/1.1 200 OK\r\n",
        b"Content-Length: 5\r\nConnection: close\r\n\r\nimage"], 2.0)
    with httpx.Client(trust_env=False) as client:
        assert posts._image(f"{scheme}://images.example/a", client) == b"image"
    assert state["reads"] == [5.0, 3.0]
    assert now[0] == 4.0
    assert state["closed"]


def test_opaque_network_stream_is_closed_when_deadline_guard_cannot_be_installed():
    closed = []

    class OpaqueStream:
        __slots__ = ()

        def read(self, max_bytes, timeout=None):
            pytest.fail("unguarded read")

        def write(self, data, timeout=None):
            pytest.fail("unguarded write")

        def close(self):
            closed.append(True)

    trace = posts._ImageTrace(posts.monotonic() + 30.0)
    with pytest.raises(ValueError, match="cannot enforce a socket deadline"):
        trace("connection.connect_tcp.complete", {"return_value": OpaqueStream()})
    assert closed == [True]


def test_unguarded_reused_connection_and_http2_are_rejected_before_headers():
    trace = posts._ImageTrace(posts.monotonic() + 30.0)
    with pytest.raises(ValueError, match="unguarded connection"):
        trace("http11.receive_response_headers.started", {})
    with pytest.raises(ValueError, match="dedicated HTTP/1.1"):
        trace("http2.send_request_headers.started", {})


@pytest.fixture
def resolver_pool(monkeypatch):
    from threading import Event

    pool = posts._ImageResolver(workers=2)
    release = Event()
    monkeypatch.setattr(posts, "_image_resolver", pool)
    yield pool, release
    # Every blocked resolver in these tests waits on this event, never real DNS.
    release.set()
    pool.jobs.join()
    for _ in pool.threads:
        pool.jobs.put(None)
    for thread in pool.threads:
        thread.join(timeout=1.0)
        assert not thread.is_alive()


def test_dns_timeout_returns_while_resolver_is_blocked_and_late_answer_never_fetches(monkeypatch, resolver_pool):
    from threading import Event, Thread

    pool, release = resolver_pool
    entered, returned = Event(), Event()
    calls, result = [], []
    monkeypatch.setattr(posts, "IMAGE_SECONDS", 0.1)

    def resolve(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=2.0)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, 443))]

    monkeypatch.setattr(posts.socket, "getaddrinfo", resolve)

    def fetch():
        with httpx.Client(transport=httpx.MockTransport(lambda r: calls.append(r))) as client:
            result.append(posts._image("https://images.example/a", client))
        returned.set()

    thread = Thread(target=fetch, daemon=True)
    thread.start()
    try:
        assert entered.wait(timeout=1.0)
        assert returned.wait(timeout=1.0)  # No resolver/executor shutdown join.
        assert not release.is_set()
        assert result == [None]
        assert calls == []
    finally:
        release.set()
        thread.join(timeout=1.0)
        pool.jobs.join()
    assert calls == []  # A late public answer cannot continue the timed-out fetch.


def test_repeated_dns_timeouts_keep_only_two_workers_and_no_pending_queue(monkeypatch, resolver_pool):
    from concurrent.futures import Future
    from threading import Event, Lock

    pool, release = resolver_pool
    entered = [Event(), Event()]
    futures, dns_calls, http_calls = [], [], []
    lock = Lock()

    class TimedFuture(Future):
        def __init__(self):
            super().__init__()
            self.index = len(futures)
            futures.append(self)

        def result(self, timeout=None):
            assert timeout <= posts.IMAGE_SECONDS
            assert entered[self.index].wait(timeout=1.0)
            raise TimeoutError("mock resolver wait deadline")

    def resolve(host, port, **kwargs):
        with lock:
            index = len(dns_calls)
            dns_calls.append(host)
        entered[index].set()
        assert release.wait(timeout=2.0)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, port))]

    monkeypatch.setattr(posts, "Future", TimedFuture)
    monkeypatch.setattr(posts.socket, "getaddrinfo", resolve)
    try:
        with httpx.Client(transport=httpx.MockTransport(lambda r: http_calls.append(r))) as client:
            for index in range(20):
                assert posts._image(f"https://images{index}.example/a", client) is None
        assert len(pool.threads) == 2
        assert all(thread.daemon for thread in pool.threads)
        assert len(dns_calls) == len(futures) == 2
        assert pool.jobs.qsize() == 0
        assert http_calls == []
    finally:
        release.set()
        pool.jobs.join()
    assert all(future.done() for future in futures)
    assert http_calls == []
    # Finished late work releases capacity rather than permanently disabling DNS.
    monkeypatch.setattr(posts, "Future", Future)
    dns(monkeypatch, [PUBLIC])
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"image"))) as client:
        assert posts._image("https://images.example/a", client) == b"image"


def _small_png():
    out = io.BytesIO()
    Image.new("RGB", (10, 10), "navy").save(out, "PNG")
    return out.getvalue()


def _post_with_images(client, count):
    meta = {"title": "Test", "authors": [], "year": 2026, "venue": "LessWrong", "source": {"url": ""}}
    html = "<p>Before</p>" + "".join(
        f'<img src="https://images.example/{i}.png" alt="figure{i}">' for i in range(count)) + "<p>After</p>"
    data = posts.render(meta, html, client)
    return " ".join(" ".join(page.extract_text() for page in PdfReader(io.BytesIO(data)).pages).split())


def test_whole_post_count_limit_skips_further_requests_and_writes_placeholders(monkeypatch):
    dns(monkeypatch, [PUBLIC])
    monkeypatch.setattr(posts, "POST_IMAGES", 2)
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, content=_small_png())

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        text = _post_with_images(client, 5)
    assert len(calls) == 2
    assert all(f"[image: figure{i}] (post image count budget exhausted)" in text for i in [2, 3, 4])
    assert "Before" in text and "After" in text


def test_whole_post_byte_limit_counts_failed_images_and_skips_later_requests(monkeypatch):
    dns(monkeypatch, [PUBLIC])
    monkeypatch.setattr(posts, "POST_IMAGE_BYTES", 6)
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, content=b"bad!")  # Small, intentionally unreadable image.

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        text = _post_with_images(client, 4)
    assert len(calls) == 2
    assert "[image: figure0]" in text
    assert all(f"[image: figure{i}] (post image byte budget exhausted)" in text for i in [1, 2, 3])
    assert "Before" in text and "After" in text


def test_whole_post_deadline_is_shared_instead_of_reset_for_each_image(monkeypatch):
    dns(monkeypatch, [PUBLIC])
    now = [0.0]
    monkeypatch.setattr(posts, "monotonic", lambda: now[0])
    monkeypatch.setattr(posts, "POST_IMAGE_SECONDS", 5.0)
    calls = []

    def handle(request):
        calls.append(request)
        now[0] += 3.0
        return httpx.Response(200, content=_small_png())

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        text = _post_with_images(client, 4)
    assert len(calls) == 2
    assert calls[1].extensions["timeout"]["read"] == 2.0
    assert all(f"[image: figure{i}] (post image time budget exhausted)" in text for i in [1, 2, 3])
    assert "Before" in text and "After" in text


def test_shared_decoded_byte_budget_also_limits_compressed_images(monkeypatch):
    import gzip

    dns(monkeypatch, [PUBLIC])
    budget = posts._PostImageBudget(posts.monotonic() + 30.0, 4, 4, 100)
    calls = []

    class Compressed(httpx.SyncByteStream):
        def __iter__(self):
            yield gzip.compress(b"12345")

    def handle(request):
        calls.append(request)
        return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=Compressed())

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert posts._image("https://images.example/a", client, budget=budget) is None
        assert posts._image("https://images.example/b", client, budget=budget) is None
    assert len(calls) == 1
    assert budget.bytes_left == 0
    assert budget.reason == "post image byte budget exhausted"


def test_socket_receive_size_is_clamped_to_shared_remaining_byte_budget():
    reads = []

    class Stream:
        def read(self, max_bytes, timeout=None):
            reads.append(max_bytes)
            return b"x" * max_bytes

        def write(self, data, timeout=None):
            pass

        def close(self):
            pass

    budget = posts._PostImageBudget(posts.monotonic() + 30.0, 1, 100, 3)
    stream = Stream()
    trace = posts._ImageTrace(budget.deadline, budget)
    trace("connection.connect_tcp.complete", {"return_value": stream})
    assert stream.read(65536, timeout=20.0) == b"xxx"
    with pytest.raises(ValueError, match="byte budget exhausted"):
        stream.read(65536, timeout=20.0)
    assert reads == [3]

def test_stalled_dns_is_abandoned_at_the_deadline(monkeypatch):
    import threading
    import time

    release = threading.Event()
    monkeypatch.setattr(posts.socket, "getaddrinfo", lambda *a, **k: release.wait(10) and [])
    monkeypatch.setattr(posts, "IMAGE_SECONDS", 0.2)
    started = time.monotonic()
    try:
        with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("unexpected HTTP"))) as client:
            assert posts._image("https://images.example/a", client) is None
        assert time.monotonic() - started < 2
    finally:
        release.set()


def test_image_takes_the_post_deadline_when_it_is_sooner(monkeypatch):
    dns(monkeypatch, [PUBLIC])
    now = [0.0]
    monkeypatch.setattr(posts, "monotonic", lambda: now[0])
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, content=b"image")

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert posts._image("https://images.example/a", client, until=3.0) == b"image"
        assert posts._image("https://images.example/a", client, until=0.0) is None
    assert len(calls) == 1
    assert calls[0].extensions["timeout"]["read"] == 3.0


POST_META = {"title": "Test", "authors": [], "year": 2026, "venue": "LessWrong",
             "source": {"url": "https://www.lesswrong.com/posts/example"}}


def render_images(monkeypatch, count, fetch):
    html = "".join(f'<img src="https://images.example/{i}" alt="figure {i}">' for i in range(count))
    monkeypatch.setattr(posts, "_image", lambda src, client, until, **kwargs: fetch(src, client, until))
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("unexpected HTTP"))) as client:
        rendered = posts.render(POST_META, html, client)
    return " ".join(page.extract_text() for page in PdfReader(io.BytesIO(rendered)).pages)


def test_images_past_the_post_cap_are_placeholders(monkeypatch):
    monkeypatch.setattr(posts, "POST_IMAGES", 2)
    fetched = []
    text = render_images(monkeypatch, 3, lambda src, client, until: fetched.append(src))
    assert fetched == ["https://images.example/0", "https://images.example/1"]
    assert "[image: figure 2]" in text


def test_images_share_one_post_deadline(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(posts, "monotonic", lambda: now[0])
    monkeypatch.setattr(posts, "POST_IMAGE_SECONDS", 5.0)
    deadlines = []

    def slow(src, client, until):
        deadlines.append(until)
        now[0] += 2.0

    text = render_images(monkeypatch, 5, slow)
    assert deadlines == [5.0, 5.0, 5.0]
    assert "[image: figure 3]" in text and "[image: figure 4]" in text


def _compressed_fixture(payload, encoding):
    import gzip
    import zlib

    if encoding == "gzip":
        return gzip.compress(payload)
    return zlib.compress(payload, wbits=-zlib.MAX_WBITS if encoding == "raw-deflate" else zlib.MAX_WBITS)


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "raw-deflate"])
def test_decompressed_allocation_is_bounded_before_budget_check(monkeypatch, encoding):
    dns(monkeypatch, [PUBLIC])
    payload = _compressed_fixture(b"x" * 4096, encoding)  # Inert, small expansion fixture.
    allocations = []
    original = posts.zlib.decompressobj

    class GuardedDecoder:
        def __init__(self, *args, **kwargs):
            self.decoder = original(*args, **kwargs)

        def decompress(self, data, max_length=0):
            assert 0 < max_length <= 32
            value = self.decoder.decompress(data, max_length=max_length)
            allocations.append((max_length, len(value)))
            return value

        def flush(self, *args, **kwargs):
            pytest.fail("unbounded decoder flush")

        def __getattr__(self, name):
            return getattr(self.decoder, name)

    monkeypatch.setattr(posts.zlib, "decompressobj", GuardedDecoder)
    monkeypatch.setattr(httpx.Response, "iter_bytes", lambda *a, **k: pytest.fail("eager HTTPX decoding"))
    budget = posts._PostImageBudget(posts.monotonic() + 30.0, 2, 31, 1000)

    class Body(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            yield payload

        def close(self):
            self.closed = True

    stream = Body()
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, headers={"content-encoding": "gzip" if encoding == "gzip" else "deflate"}, stream=stream)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert posts._image("https://images.example/a", client, budget=budget) is None
        assert posts._image("https://images.example/b", client, budget=budget) is None
    assert allocations == [(32, 32)]
    assert budget.reason == "post image byte budget exhausted"
    assert len(calls) == 1
    assert calls[0].headers["accept-encoding"] == "gzip, deflate"
    assert stream.closed


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "raw-deflate"])
@pytest.mark.parametrize("fragment", [1, 3, 1000])
def test_valid_compression_survives_fragmentation_and_bounded_drain(monkeypatch, encoding, fragment):
    dns(monkeypatch, [PUBLIC])
    expected = b"image bytes " * 20
    payload = _compressed_fixture(expected, encoding)
    monkeypatch.setattr(posts, "IMAGE_DECODE_CHUNK", 17)
    monkeypatch.setattr(httpx.Response, "_get_content_decoder", lambda *a: pytest.fail("HTTPX decoder must not run"))

    class Body(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            for offset in range(0, len(payload), fragment):
                yield payload[offset:offset + fragment]

        def close(self):
            self.closed = True

    stream = Body()
    budget = posts._PostImageBudget(posts.monotonic() + 30.0, 1, len(expected), 1000)
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(
            200, headers={"content-encoding": "gzip" if encoding == "gzip" else "deflate"}, stream=stream))) as client:
        assert posts._image("https://images.example/a", client, budget=budget) == expected
    assert budget.bytes_left == 0
    assert stream.closed


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "raw-deflate"])
def test_individual_image_limit_bounds_decompression_without_post_budget(monkeypatch, encoding):
    dns(monkeypatch, [PUBLIC])
    monkeypatch.setattr(posts, "IMAGE_BYTES", 31)
    payload = _compressed_fixture(b"x" * 1024, encoding)
    limits = []
    original = posts.zlib.decompressobj

    class Decoder:
        def __init__(self, *args, **kwargs):
            self.decoder = original(*args, **kwargs)

        def decompress(self, data, max_length=0):
            limits.append(max_length)
            assert 0 < max_length <= 32
            return self.decoder.decompress(data, max_length=max_length)

        def __getattr__(self, name):
            return getattr(self.decoder, name)

    monkeypatch.setattr(posts.zlib, "decompressobj", Decoder)

    class Body(httpx.SyncByteStream):
        def __iter__(self):
            yield payload

    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(
            200, headers={"content-encoding": "gzip" if encoding == "gzip" else "deflate"}, stream=Body()))) as client:
        assert posts._image("https://images.example/a", client) is None
    assert limits == [32]


@pytest.mark.parametrize("split_members", [False, True])
def test_concatenated_gzip_members_still_obey_total_decoded_limit(monkeypatch, split_members):
    dns(monkeypatch, [PUBLIC])
    first = _compressed_fixture(b"first", "gzip")
    second = _compressed_fixture(b"second", "gzip")
    expected = b"firstsecond"
    monkeypatch.setattr(posts, "IMAGE_DECODE_CHUNK", 3)

    class Body(httpx.SyncByteStream):
        def __iter__(self):
            yield first if split_members else first + second
            if split_members:
                yield second

    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(
            200, headers={"content-encoding": "gzip"}, stream=Body()))) as client:
        assert posts._image("https://images.example/a", client) == expected


@pytest.mark.parametrize("encoding,payload", [
    ("gzip", b"invalid"),
    ("deflate", b"invalid"),
    ("br", b"inert"),
    ("zstd", b"inert"),
    ("gzip, deflate", b"inert"),
    ("gzip", _compressed_fixture(b"image", "gzip")[:-2]),
    ("deflate", _compressed_fixture(b"image", "deflate")[:-2]),
])
def test_malformed_truncated_and_unsupported_encodings_leave_placeholder(monkeypatch, encoding, payload):
    dns(monkeypatch, [PUBLIC])
    monkeypatch.setattr(httpx.Response, "_get_content_decoder", lambda *a: pytest.fail("HTTPX decoder must not run"))

    class Body(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            yield payload

        def close(self):
            self.closed = True

    stream = Body()
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(
            200, headers={"content-encoding": encoding}, stream=stream))) as client:
        assert posts._image("https://images.example/a", client) is None
    assert stream.closed
