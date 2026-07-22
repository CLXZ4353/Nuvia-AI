from src.tools.urls import canonicalize_url, is_allowed_public_url


def test_canonicalize_url_removes_tracking_fragment_and_default_port():
    assert canonicalize_url(
        "HTTPS://www.ArXiv.org:443/abs/1234/?utm_source=x&v=2#section"
    ) == "https://arxiv.org/abs/1234?v=2"


def test_is_allowed_public_url_requires_http_allowlist_and_public_host():
    allowed = {"arxiv.org"}

    assert is_allowed_public_url("https://export.arxiv.org/abs/1", allowed)
    assert not is_allowed_public_url("https://example.com/abs/1", allowed)
    assert not is_allowed_public_url("file:///tmp/paper", allowed)
    assert not is_allowed_public_url("http://127.0.0.1/private", {"127.0.0.1"})
