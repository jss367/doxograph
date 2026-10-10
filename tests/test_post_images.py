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
    monkeypatch.setattr(posts, "_image", lambda *a: data.getvalue())
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


def test_https_proxy_keeps_pinned_destination_and_certificate_errors_fail_closed(monkeypatch):
    """Exercise CONNECT without a proxy or socket, including TLS-name support."""
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
            tls.append(server_hostname)
            # A typical public CDN certificate covers its DNS name, not its IP.
            if server_hostname != "images.example":
                raise ssl.SSLCertVerificationError("mock certificate does not cover pinned IP")
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
    with httpx.Client(proxy="http://proxy.example:8080", trust_env=False) as client:
        data = posts._image("https://images.example/a", client)
    assert connections == [("proxy.example", 8080)]
    assert b"CONNECT " + PUBLIC.encode() + b":443 HTTP/1.1" in b"".join(writes)
    if tls == [PUBLIC]:
        # httpcore 1.0.9 does not honor sni_hostname for CONNECT tunnels.
        assert data is None
        assert b"GET " not in b"".join(writes)
    else:
        assert tls == ["images.example"]
        assert data == b"image"
