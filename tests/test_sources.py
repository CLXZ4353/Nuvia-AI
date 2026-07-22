from pathlib import Path

import pytest
import yaml

from src.tools.sources import (
    _discover_feed_url,
    _filter_authoritative_sources,
    resolve_sources,
)


def test_filter_authoritative_sources_rejects_unlisted_domain():
    sources = [
        {
            "id": "good",
            "type": "rss",
            "url": "https://arxiv.org/rss/cs.AI",
            "trust_score": 5,
        },
        {
            "id": "bad",
            "type": "rss",
            "url": "https://random-blog.example/feed.xml",
            "trust_score": 5,
        },
    ]

    filtered = _filter_authoritative_sources(
        sources,
        {
            "authoritative_domains": ["arxiv.org"],
            "min_trust_score": 4,
            "allow_unlisted_domains": False,
        },
    )

    assert [source["id"] for source in filtered] == ["good"]


def test_filter_authoritative_sources_accepts_allowed_subdomain():
    sources = [
        {
            "id": "midjourney_updates",
            "type": "rss",
            "url": "https://updates.midjourney.com/rss/",
            "trust_score": 4,
        }
    ]

    filtered = _filter_authoritative_sources(
        sources,
        {
            "authoritative_domains": ["midjourney.com"],
            "min_trust_score": 4,
            "allow_unlisted_domains": False,
        },
    )

    assert [source["id"] for source in filtered] == ["midjourney_updates"]


def test_resolve_sources_loads_catalog_and_dedupes_inline_sources(tmp_path):
    catalog_path = tmp_path / "source_catalog.yaml"
    catalog_path.write_text(
        yaml.safe_dump(
            {
                "sources": [
                    {
                        "id": "arxiv_ai",
                        "name": "arXiv",
                        "type": "rss",
                        "url": "https://rss.arxiv.org/rss/cs.AI",
                        "trust_score": 5,
                        "priority": "high",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text("beat: test\n", encoding="utf-8")
    config = {
        "source_catalog": {
            "path": str(catalog_path),
            "authoritative_domains": ["rss.arxiv.org"],
            "min_trust_score": 4,
        },
        "sources": [
            {
                "id": "arxiv_ai",
                "name": "Duplicate",
                "type": "rss",
                "url": "https://rss.arxiv.org/rss/cs.AI",
                "trust_score": 5,
            }
        ],
    }

    sources = resolve_sources(config, str(config_path))

    assert len(sources) == 1
    assert sources[0]["id"] == "arxiv_ai"


def test_discover_feed_url_from_alternate_link(monkeypatch):
    class FakeResponse:
        text = '<html><head><link rel="alternate" type="application/rss+xml" href="/feed.xml"></head></html>'

        def raise_for_status(self):
            return None

    monkeypatch.setattr("src.tools.sources.httpx.get", lambda *args, **kwargs: FakeResponse())

    assert _discover_feed_url("https://example.com/blog/") == "https://example.com/feed.xml"


def test_resolve_sources_fails_early_when_configured_catalog_is_unavailable(tmp_path):
    config = {
        "source_catalog": {
            "path": "missing.yaml",
            "authoritative_domains": ["arxiv.org"],
        },
        "sources": [],
    }

    with pytest.raises(ValueError, match="Impossibile caricare fonti"):
        resolve_sources(config, str(tmp_path / "config.yaml"))
