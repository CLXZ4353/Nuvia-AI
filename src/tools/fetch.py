from __future__ import annotations

import logging
import hashlib
import json
import re
import time
import warnings
from datetime import datetime
from typing import NotRequired, TypedDict
from urllib.parse import urljoin, urlparse

import feedparser
import httpx
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

from src.tools.urls import is_allowed_public_url

logger = logging.getLogger(__name__)

USER_AGENT = "research-digest-agent/1.0 (+https://example.local)"
MIN_PAGE_TEXT_LENGTH = 200
MAX_PAGE_TEXT_LENGTH = 20_000
DEFAULT_HTTP_RETRIES = 2
DEFAULT_RETRY_DELAY_SECONDS = 1
DEFAULT_SCRAPE_SELECTOR = "main article, main h2 a[href], main h3 a[href], article a[href]"


class RawItem(TypedDict):
    title: str
    url: str
    source_id: str
    source_name: str
    source_group: str | None
    snippet: str | None
    pub_date: str | None
    # Per gli item RSS l'URL dell'articolo resta la destinazione del digest,
    # mentre il feed e' la fonte da usare per la verifica del contenuto.
    _verification_url: NotRequired[str]


def fetch_rss(source: dict) -> list[RawItem]:
    try:
        resp = _get_with_retries(source["url"])
        if (
            not source.get("allow_external_redirect", False)
            and not _same_site(source["url"], str(resp.url))
        ):
            logger.warning(
                "RSS scartato per redirect esterno: %s -> %s",
                source["url"],
                resp.url,
            )
            return []
        feed_url = str(resp.url)
        feed = feedparser.parse(resp.text)
        if getattr(feed, "bozo", False):
            logger.warning("RSS feed con warning per %s: %s", source["id"], feed.bozo_exception)
        items = []
        for entry in feed.entries[: source.get("max_items", 20)]:
            items.append(
                RawItem(
                    title=entry.get("title", ""),
                    url=entry.get("link", ""),
                    source_id=source["id"],
                    source_name=source["name"],
                    source_group=source.get("source_group"),
                    snippet=entry.get("summary", None),
                    pub_date=_entry_publication_date(entry),
                    _verification_url=feed_url,
                )
            )
        return items
    except Exception as exc:
        logger.error("RSS fetch fallito per %s: %s", source["id"], exc)
        return []


def fetch_scrape(source: dict) -> list[RawItem]:
    try:
        resp = _get_with_retries(source["url"])
        if (
            not source.get("allow_external_redirect", False)
            and not _same_site(source["url"], str(resp.url))
        ):
            logger.warning(
                "Scrape scartato per redirect esterno: %s -> %s",
                source["url"],
                resp.url,
            )
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        if source.get("check_for_changes", False):
            return _changed_page_item(source, soup)

        selector = source.get("selector") or DEFAULT_SCRAPE_SELECTOR
        nodes = soup.select(selector)
        items: list[RawItem] = []
        seen: set[str] = set()
        for node in nodes:
            link, title = _extract_item_link_and_title(node, source)
            if not link or not link.get("href"):
                continue
            url = urljoin(source["url"], link["href"])
            if url in seen or url.startswith(("mailto:", "javascript:")) or "#" in link.get("href", ""):
                continue
            if len(title) < 8:
                continue
            seen.add(url)
            items.append(
                RawItem(
                    title=title[:200],
                    url=url,
                    source_id=source["id"],
                    source_name=source["name"],
                    source_group=source.get("source_group"),
                    snippet=" ".join(node.get_text(" ", strip=True).split())[:500] or None,
                    pub_date=_extract_publication_date(soup, node),
                )
            )
            if len(items) >= source.get("max_items", 20):
                break
        return items
    except Exception as exc:
        logger.error("Scrape fallito per %s: %s", source["id"], exc)
        return []


def fetch_sources(config: dict) -> tuple[list[RawItem], list[str]]:
    raw_items: list[RawItem] = []
    failed_sources: list[str] = []
    for source in config["sources"]:
        logger.info("Fonte %s: raccolta da %s", source["id"], source["url"])
        source_type = source.get("type")
        if source_type == "rss":
            items = fetch_rss(source)
        elif source_type == "scrape":
            items = fetch_scrape(source)
        else:
            logger.warning("Tipo fonte non supportato per %s: %s", source["id"], source_type)
            items = []
        if not items:
            failed_sources.append(source["id"])
            logger.warning("Fonte %s: nessun item raccolto", source["id"])
        else:
            logger.info("Fonte %s: %s item raccolti", source["id"], len(items))
        raw_items.extend(items)
    return raw_items, failed_sources


def fetch_url(url: str, allowed_domains: set[str] | None = None) -> tuple[bool, str]:
    try:
        resp = _get_with_retries(url)
        resp.raise_for_status()
        if allowed_domains and not is_allowed_public_url(str(resp.url), allowed_domains):
            return False, "Redirect verso un dominio non autorizzato"

        content_type = resp.headers.get("content-type", "").split(";", 1)[0].lower()
        supported_type = (
            not content_type
            or content_type.startswith("text/")
            or content_type
            in {
                "application/xhtml+xml",
                "application/xml",
                "application/rss+xml",
                "application/atom+xml",
                "application/json",
            }
        )
        if not supported_type:
            return False, f"Content-Type non verificabile: {content_type}"

        with warnings.catch_warnings():
            if content_type in {"text/xml", "application/xml", "application/rss+xml", "application/atom+xml"}:
                warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
            soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
        normalized_text = " ".join(text.split())
        if len(normalized_text) < MIN_PAGE_TEXT_LENGTH:
            return False, f"Contenuto testuale insufficiente ({len(normalized_text)} caratteri)"
        return True, normalized_text[:MAX_PAGE_TEXT_LENGTH]
    except httpx.HTTPStatusError as exc:
        return False, f"HTTP {exc.response.status_code}"
    except Exception as exc:
        return False, str(exc)


def _get_with_retries(url: str, attempts: int = DEFAULT_HTTP_RETRIES + 1):
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = httpx.get(
                url,
                timeout=15,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            )
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            last_error = exc
            if exc.response.status_code not in {408, 429, 500, 502, 503, 504}:
                raise
        except httpx.RequestError as exc:
            last_error = exc
        if attempt < attempts - 1:
            time.sleep(DEFAULT_RETRY_DELAY_SECONDS * (2**attempt))
    assert last_error is not None
    raise last_error


def _same_site(source_url: str, final_url: str) -> bool:
    source_host = _normalized_host(source_url)
    final_host = _normalized_host(final_url)
    return source_host == final_host or final_host.endswith(f".{source_host}")


def _normalized_host(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def _extract_item_link_and_title(node, source: dict):
    title_selector = source.get("title_selector")
    if title_selector:
        title_node = node.select_one(title_selector)
        if title_node:
            link = title_node if title_node.name == "a" else title_node.find("a", href=True)
            fallback_link = node if node.name == "a" else node.find("a", href=True)
            return link or fallback_link, _clean_title_text(title_node.get_text(" ", strip=True))

    if node.name == "a":
        return node, _clean_title_text(node.get_text(" ", strip=True))

    for selector in ("h1 a[href]", "h2 a[href]", "h3 a[href]", "h4 a[href]"):
        link = node.select_one(selector)
        if link:
            return link, _clean_title_text(link.get_text(" ", strip=True))

    heading = node.find(["h1", "h2", "h3", "h4"])
    link = heading.find("a", href=True) if heading else None
    if heading and link:
        return link, _clean_title_text(heading.get_text(" ", strip=True))

    link = node.find("a", href=True)
    return link, _clean_title_text((link.get_text(" ", strip=True) if link else "") or node.get_text(" ", strip=True))


def _clean_title_text(value: str) -> str:
    text = " ".join(value.split())
    text = re.sub(r"\b(learn more|read more)\b", "", text, flags=re.IGNORECASE).strip(" -:")
    return text[:200]


def _changed_page_item(source: dict, soup: BeautifulSoup) -> list[RawItem]:
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        tag.decompose()
    content_node = soup.find("main") or soup.body or soup
    text = " ".join(content_node.get_text(" ", strip=True).split())
    if len(text) < MIN_PAGE_TEXT_LENGTH:
        return []
    title_node = soup.find("h1") or soup.find("title")
    title = " ".join(title_node.get_text(" ", strip=True).split()) if title_node else source["name"]
    return [
        RawItem(
            title=title[:200] or source["name"],
            url=source["url"],
            source_id=source["id"],
            source_name=source["name"],
            source_group=source.get("source_group"),
            snippet=text[:500],
            pub_date=_extract_publication_date(soup),
            _content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
    ]


def _entry_publication_date(entry) -> str | None:
    for field_name in ("published", "updated", "created", "issued", "date"):
        value = entry.get(field_name)
        if value:
            return str(value).strip()

    for field_name in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(field_name)
        if value:
            try:
                return datetime(*value[:6]).date().isoformat()
            except (TypeError, ValueError):
                continue
    return None


def _extract_publication_date(soup: BeautifulSoup, node=None) -> str | None:
    for root in _date_roots(soup, node):
        value = _date_from_time_tag(root)
        if value:
            return value
        value = _date_from_meta_tag(root)
        if value:
            return value
        value = _date_from_json_ld(root)
        if value:
            return value
        value = _date_from_visible_attributes(root)
        if value:
            return value
        value = _date_from_visible_text(root)
        if value:
            return value
    return None


def _date_roots(soup: BeautifulSoup, node=None) -> list:
    if node is None:
        return [soup]
    return [node]


def _date_from_time_tag(root) -> str | None:
    for tag in root.select("time"):
        value = tag.get("datetime") or tag.get("content") or tag.get_text(" ", strip=True)
        if value:
            return " ".join(str(value).split())
    return None


def _date_from_meta_tag(root) -> str | None:
    selectors = [
        "meta[property='article:published_time']",
        "meta[property='article:modified_time']",
        "meta[property='og:updated_time']",
        "meta[name='date']",
        "meta[name='publishdate']",
        "meta[name='pubdate']",
        "meta[name='DC.date']",
        "meta[itemprop='datePublished']",
        "meta[itemprop='dateModified']",
    ]
    for selector in selectors:
        tag = root.select_one(selector)
        if tag and tag.get("content"):
            return " ".join(tag["content"].split())
    return None


def _date_from_json_ld(root) -> str | None:
    for script in root.select("script[type='application/ld+json']"):
        try:
            payload = json.loads(script.string or script.get_text() or "")
        except json.JSONDecodeError:
            continue
        value = _find_json_date(payload)
        if value:
            return value
    return None


def _find_json_date(payload) -> str | None:
    if isinstance(payload, list):
        for item in payload:
            value = _find_json_date(item)
            if value:
                return value
    if isinstance(payload, dict):
        for key in ("datePublished", "dateModified", "uploadDate", "releaseDate"):
            value = payload.get(key)
            if value:
                return " ".join(str(value).split())
        for key in ("@graph", "mainEntity", "itemListElement"):
            value = _find_json_date(payload.get(key))
            if value:
                return value
    return None


def _date_from_visible_attributes(root) -> str | None:
    for tag in root.select("[class*='date'], [class*='time'], [class*='published'], [class*='post-meta']"):
        text = " ".join(tag.get_text(" ", strip=True).split())
        if re.search(r"\b\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}\b", text) or re.search(
            r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|gen|feb|mar|apr|mag|giu|lug|ago|set|ott|nov|dic)",
            text,
            flags=re.IGNORECASE,
        ):
            return text[:120]
    return None


def _date_from_visible_text(root) -> str | None:
    text = " ".join(root.get_text(" ", strip=True).split())
    patterns = [
        r"\b(?:Published on|Published|Updated on|Updated)\s+([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})\b",
        r"\b([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})\b",
        r"\b(\d{4}[-/.]\d{1,2}[-/.]\d{1,2})\b",
        r"\b(\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})\b",
        r"\b(\d{1,2}\s+(?:gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|settembre|ottobre|novembre|dicembre)\s+\d{4})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None
