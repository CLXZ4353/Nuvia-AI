import httpx

from src.tools.fetch import MAX_PAGE_TEXT_LENGTH, fetch_rss, fetch_scrape, fetch_url


class FakeResponse:
    def __init__(self, text: str, url: str, content_type: str = "text/html"):
        self.text = text
        self.url = url
        self.headers = {"content-type": content_type}

    def raise_for_status(self):
        return None


def test_fetch_url_requires_sufficient_public_text(monkeypatch):
    monkeypatch.setattr(
        "src.tools.fetch.httpx.get",
        lambda *args, **kwargs: FakeResponse("short", "https://arxiv.org/abs/1"),
    )

    ok, reason = fetch_url("https://arxiv.org/abs/1", {"arxiv.org"})

    assert not ok
    assert "insufficiente" in reason


def test_fetch_url_rejects_redirect_to_unlisted_domain(monkeypatch):
    monkeypatch.setattr(
        "src.tools.fetch.httpx.get",
        lambda *args, **kwargs: FakeResponse("x" * 300, "https://evil.example/paper"),
    )

    ok, reason = fetch_url("https://arxiv.org/abs/1", {"arxiv.org"})

    assert not ok
    assert "dominio non autorizzato" in reason


def test_fetch_url_returns_normalized_verified_text(monkeypatch):
    html = f"<main><h1>Paper</h1><p>{'verified research content ' * 20}</p></main>"
    monkeypatch.setattr(
        "src.tools.fetch.httpx.get",
        lambda *args, **kwargs: FakeResponse(html, "https://arxiv.org/abs/1"),
    )

    ok, content = fetch_url("https://arxiv.org/abs/1", {"arxiv.org"})

    assert ok
    assert content.startswith("Paper verified research content")


def test_fetch_rss_uses_http_client_and_extracts_items(monkeypatch):
    captured = {}
    rss = """<?xml version="1.0"?>
    <rss version="2.0"><channel><title>Feed</title>
      <item>
        <title>Research item</title>
        <link>https://example.com/research</link>
        <pubDate>Tue, 07 Jul 2026 08:00:00 GMT</pubDate>
        <description>Useful details.</description>
      </item>
    </channel></rss>"""

    def fake_get(*args, **kwargs):
        captured.update(kwargs)
        return FakeResponse(rss, "https://example.com/feed.xml", "application/rss+xml")

    monkeypatch.setattr("src.tools.fetch.httpx.get", fake_get)

    items = fetch_rss(
        {
            "id": "feed",
            "name": "Feed",
            "url": "https://example.com/feed.xml",
            "max_items": 10,
        }
    )

    assert captured["timeout"] == 15
    assert "User-Agent" in captured["headers"]
    assert items[0]["title"] == "Research item"
    assert items[0]["url"] == "https://example.com/research"
    assert items[0]["_verification_url"] == "https://example.com/feed.xml"
    assert items[0]["pub_date"] == "Tue, 07 Jul 2026 08:00:00 GMT"


def test_fetch_rss_rejects_external_redirect(monkeypatch):
    monkeypatch.setattr(
        "src.tools.fetch.httpx.get",
        lambda *args, **kwargs: FakeResponse("<rss></rss>", "https://other.example/feed.xml", "application/rss+xml"),
    )

    items = fetch_rss(
        {
            "id": "feed",
            "name": "Feed",
            "url": "https://example.com/feed.xml",
            "max_items": 10,
        }
    )

    assert items == []


def test_fetch_url_accepts_rss_content_as_a_verification_source(monkeypatch):
    rss = """<?xml version="1.0"?>
    <rss version="2.0"><channel><title>Feed</title>
      <item>
        <title>Research item</title>
        <description>{}</description>
      </item>
    </channel></rss>""".format("verified RSS evidence " * 20)
    monkeypatch.setattr(
        "src.tools.fetch.httpx.get",
        lambda *args, **kwargs: FakeResponse(rss, "https://example.com/feed.xml", "application/rss+xml"),
    )

    ok, content = fetch_url("https://example.com/feed.xml", {"example.com"})

    assert ok
    assert "verified RSS evidence" in content


def test_fetch_url_retries_transient_network_error(monkeypatch):
    calls = 0

    def fake_get(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("temporary", request=httpx.Request("GET", args[0]))
        return FakeResponse("verified content " * 30, args[0])

    monkeypatch.setattr("src.tools.fetch.httpx.get", fake_get)
    monkeypatch.setattr("src.tools.fetch.time.sleep", lambda *_: None)

    assert fetch_url("https://arxiv.org/abs/1", {"arxiv.org"})[0]
    assert calls == 2


def test_fetch_url_keeps_evidence_beyond_old_limit(monkeypatch):
    html = "<main>" + ("a" * 6000) + " deep evidence " + ("b" * 300) + "</main>"
    monkeypatch.setattr(
        "src.tools.fetch.httpx.get",
        lambda *args, **kwargs: FakeResponse(html, "https://arxiv.org/abs/1"),
    )

    ok, content = fetch_url("https://arxiv.org/abs/1", {"arxiv.org"})

    assert ok and "deep evidence" in content
    assert len(content) <= MAX_PAGE_TEXT_LENGTH


def test_change_only_source_returns_page_fingerprint(monkeypatch):
    html = "<html><title>Leaderboard</title><main>" + ("ranking data " * 30) + "</main></html>"
    monkeypatch.setattr(
        "src.tools.fetch.httpx.get",
        lambda *args, **kwargs: FakeResponse(html, "https://lmarena.ai/"),
    )
    source = {
        "id": "arena",
        "name": "Arena",
        "url": "https://lmarena.ai/",
        "check_for_changes": True,
    }

    items = fetch_scrape(source)

    assert items[0]["url"] == source["url"]
    assert len(items[0]["_content_hash"]) == 64


def test_fetch_scrape_reads_item_date_from_time_tag(monkeypatch):
    html = """
    <main>
      <article>
        <a href="/post">Research update</a>
        <time datetime="02/07/2026">2 luglio 2026</time>
        <p>Details about the research update.</p>
      </article>
    </main>
    """
    monkeypatch.setattr(
        "src.tools.fetch.httpx.get",
        lambda *args, **kwargs: FakeResponse(html, "https://example.com/blog"),
    )
    source = {"id": "blog", "name": "Blog", "url": "https://example.com/blog", "type": "scrape"}

    items = fetch_scrape(source)

    assert items[0]["pub_date"] == "02/07/2026"


def test_fetch_scrape_rejects_external_redirect(monkeypatch):
    monkeypatch.setattr(
        "src.tools.fetch.httpx.get",
        lambda *args, **kwargs: FakeResponse("<main><article><a href='/x'>Other page</a></article></main>", "https://huggingface.co/papers/trending"),
    )
    source = {
        "id": "paperswithcode",
        "name": "Papers With Code",
        "url": "https://paperswithcode.com/latest",
        "type": "scrape",
    }

    assert fetch_scrape(source) == []


def test_fetch_scrape_uses_article_heading_as_title_and_visible_date(monkeypatch):
    html = """
    <main>
      <article>
        <a href="/profile">Submitted by user</a>
        <h3><a href="/papers/1">Clean English Paper Title</a></h3>
        <p>Short description of the paper.</p>
        <span>Published on Jul 2, 2026</span>
        <a href="/login">Upvote 10</a>
      </article>
    </main>
    """
    monkeypatch.setattr(
        "src.tools.fetch.httpx.get",
        lambda *args, **kwargs: FakeResponse(html, "https://huggingface.co/papers"),
    )
    source = {
        "id": "huggingface_papers",
        "name": "Hugging Face - Daily Papers",
        "url": "https://huggingface.co/papers",
        "type": "scrape",
        "selector": "article",
    }

    items = fetch_scrape(source)

    assert items[0]["title"] == "Clean English Paper Title"
    assert items[0]["url"] == "https://huggingface.co/papers/1"
    assert items[0]["pub_date"] == "Jul 2, 2026"


def test_fetch_scrape_title_selector_keeps_anchor_node_link(monkeypatch):
    html = """
    <main>
      <a href="/post">
        <span>product</span>
        <h2>Clean Product Update</h2>
        <p>Verbose teaser text.</p>
        <time datetime="2026-07-07">Jul 7, 2026</time>
      </a>
    </main>
    """
    monkeypatch.setattr(
        "src.tools.fetch.httpx.get",
        lambda *args, **kwargs: FakeResponse(html, "https://example.com/blog"),
    )
    source = {
        "id": "blog",
        "name": "Blog",
        "url": "https://example.com/blog",
        "type": "scrape",
        "selector": "main a[href]",
        "title_selector": "h2",
    }

    items = fetch_scrape(source)

    assert items[0]["title"] == "Clean Product Update"
    assert items[0]["url"] == "https://example.com/post"
    assert items[0]["pub_date"] == "2026-07-07"


def test_fetch_scrape_does_not_assign_page_level_date_to_item_without_date(monkeypatch):
    html = """
    <html>
      <head><meta property="article:published_time" content="2026-07-07"></head>
      <main>
        <article>
          <h3><a href="/post">Item without local date</a></h3>
          <p>Details about this item.</p>
        </article>
      </main>
    </html>
    """
    monkeypatch.setattr(
        "src.tools.fetch.httpx.get",
        lambda *args, **kwargs: FakeResponse(html, "https://example.com/blog"),
    )
    source = {"id": "blog", "name": "Blog", "url": "https://example.com/blog", "type": "scrape"}

    items = fetch_scrape(source)

    assert items[0]["pub_date"] is None


def test_change_only_source_reads_page_date_from_meta_tag(monkeypatch):
    html = """
    <html>
      <head>
        <title>Model release</title>
        <meta property="article:published_time" content="2 luglio 2026">
      </head>
      <main>""" + ("release details " * 30) + """</main>
    </html>
    """
    monkeypatch.setattr(
        "src.tools.fetch.httpx.get",
        lambda *args, **kwargs: FakeResponse(html, "https://example.com/release"),
    )
    source = {
        "id": "release",
        "name": "Release",
        "url": "https://example.com/release",
        "check_for_changes": True,
    }

    items = fetch_scrape(source)

    assert items[0]["pub_date"] == "2 luglio 2026"
