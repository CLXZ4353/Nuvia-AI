from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yaml
from bs4 import BeautifulSoup

from src.tools.fetch import USER_AGENT
from src.tools.urls import is_allowed_public_url

logger = logging.getLogger(__name__)

DEFAULT_SOURCE_TIMEOUT = 10


def resolve_sources(config: dict, config_path: str = "config.yaml") -> list[dict]:
    source_config = config.get("source_catalog", {})
    catalog_sources = _load_catalog_sources(source_config, config_path)
    inline_sources = config.get("sources", [])
    sources = [*catalog_sources, *inline_sources]

    if not sources:
        configured = [value for value in (source_config.get("path"), source_config.get("remote_url")) if value]
        if configured:
            raise ValueError(
                "Impossibile caricare fonti dai cataloghi configurati e nessuna fonte inline disponibile: "
                + ", ".join(configured)
            )
        raise ValueError("Nessuna fonte configurata: usa source_catalog.path, remote_url o sources.")

    filtered = _filter_authoritative_sources(sources, source_config)
    if not filtered:
        raise ValueError("Nessuna fonte pubblica autorevole supera i vincoli configurati.")
    resolved = [_resolve_source(source) for source in filtered]
    unique_sources = _dedupe_sources(resolved)
    max_sources = source_config.get("max_sources_per_run")
    if max_sources:
        unique_sources = unique_sources[:max_sources]

    logger.info(
        "Catalogo fonti: %s caricate, %s autorevoli, %s operative",
        len(sources),
        len(filtered),
        len(unique_sources),
    )
    return unique_sources


def _load_catalog_sources(source_config: dict, config_path: str) -> list[dict]:
    path = source_config.get("path")
    remote_url = source_config.get("remote_url")
    sources: list[dict] = []

    if path:
        catalog_path = _resolve_catalog_path(path, config_path)
        if catalog_path.exists():
            payload = yaml.safe_load(catalog_path.read_text(encoding="utf-8")) or {}
            sources.extend(payload.get("sources", []))
        else:
            logger.warning("Catalogo fonti non trovato: %s", catalog_path)

    if remote_url:
        try:
            response = httpx.get(
                remote_url,
                timeout=DEFAULT_SOURCE_TIMEOUT,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            )
            response.raise_for_status()
            payload = yaml.safe_load(response.text) or {}
            sources.extend(payload.get("sources", []))
        except Exception as exc:
            logger.warning("Catalogo fonti remoto non caricato (%s): %s", remote_url, exc)

    return sources


def _resolve_catalog_path(path: str, config_path: str) -> Path:
    catalog_path = Path(path)
    if catalog_path.is_absolute():
        return catalog_path
    return Path(config_path).resolve().parent / catalog_path


def _filter_authoritative_sources(sources: list[dict], source_config: dict) -> list[dict]:
    authoritative_domains = set(source_config.get("authoritative_domains", []))
    min_trust_score = source_config.get("min_trust_score", 3)
    allow_unlisted_domains = source_config.get("allow_unlisted_domains", False)

    filtered = []
    for source in sources:
        domain = _source_domain(source)
        trust_score = source.get("trust_score", 3)
        if trust_score < min_trust_score:
            logger.info("Fonte scartata per trust_score basso: %s", source.get("id", source.get("url")))
            continue
        if authoritative_domains and not _domain_allowed(domain, authoritative_domains) and not allow_unlisted_domains:
            logger.info("Fonte scartata per dominio non autorevole: %s (%s)", source.get("id"), domain)
            continue
        allowed_domains = authoritative_domains or {domain}
        if not is_allowed_public_url(source["url"], allowed_domains):
            logger.info("Fonte scartata perché non pubblica o non HTTP(S): %s", source.get("id"))
            continue
        filtered.append(source)
    return filtered


def _source_domain(source: dict) -> str:
    parsed = urlparse(source["url"])
    return parsed.netloc.lower().removeprefix("www.")


def _domain_allowed(domain: str, authoritative_domains: set[str]) -> bool:
    return any(domain == allowed or domain.endswith(f".{allowed}") for allowed in authoritative_domains)


def _resolve_source(source: dict) -> dict:
    if source.get("type") != "auto":
        return source

    resolved = dict(source)
    discovered_feed = _discover_feed_url(source["url"])
    if discovered_feed:
        resolved["type"] = "rss"
        resolved["url"] = discovered_feed
        logger.info("Fonte %s: feed RSS scoperto %s", source["id"], discovered_feed)
    else:
        resolved["type"] = "scrape"
        logger.info("Fonte %s: nessun RSS scoperto, uso scraping", source["id"])
    return resolved


def _discover_feed_url(page_url: str) -> str | None:
    try:
        response = httpx.get(
            page_url,
            timeout=DEFAULT_SOURCE_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()
    except Exception as exc:
        logger.warning("Discovery feed fallita per %s: %s", page_url, exc)
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    for link in soup.find_all("link"):
        rel = " ".join(link.get("rel", [])).lower()
        link_type = (link.get("type") or "").lower()
        href = link.get("href")
        if not href:
            continue
        if "alternate" in rel and link_type in {"application/rss+xml", "application/atom+xml"}:
            return str(httpx.URL(page_url).join(href))
    return None


def _dedupe_sources(sources: list[dict]) -> list[dict]:
    unique = []
    seen = set()
    for source in sources:
        key = source.get("id") or source.get("url")
        if key in seen:
            continue
        seen.add(key)
        unique.append(source)
    return sorted(unique, key=_source_sort_key)


def _source_sort_key(source: dict) -> tuple[int, str]:
    priority_rank = {"high": 0, "medium": 1, "low": 2}
    return priority_rank.get(source.get("priority", "medium"), 1), source.get("id", "")
