from __future__ import annotations

import argparse
import base64
import email.utils
import hashlib
import html
import json
import logging
import mimetypes
import os
import re
import smtplib
import threading
import time
import traceback
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from dotenv import load_dotenv

from src import auth
from src import notifications
from src.agent import (
    ASSISTANT_CLARIFICATION_PREFIX,
    ASSISTANT_OFF_TOPIC_ANSWER,
    AssistantAttachmentError,
    AssistantSpeechError,
    AssistantTranscriptionError,
    answer_news_question,
    synthesize_speech,
    transcribe_audio,
)
from src.firebase_push import (
    FIREBASE_JS_VERSION,
    fcm_token_registered_for_user,
    public_firebase_config,
    remove_fcm_token,
    render_firebase_messaging_service_worker,
    save_fcm_token,
)
from src.config import load_config
from src.navigation import render_sidebar, render_sidebar_css
from src.schemas import normalize_news_title
from src.tools.urls import canonicalize_url


DEFAULT_DIGEST_DIRS = ("out", "data/digests")
NEWSLETTER_SUBSCRIBERS_PATH = Path("data/newsletter_subscribers.json")
NEWSLETTER_OUTBOX_DIR = Path("data/newsletter_outbox")
NEWSLETTER_DELIVERY_STATE_PATH = Path("data/newsletter_delivery_state.json")
TELEGRAM_SUBSCRIBERS_PATH = Path("data/telegram_subscribers.json")
TELEGRAM_START_STATE_PATH = Path("data/telegram_start_state.json")
TELEGRAM_POLL_LOCK_PATH = Path("data/telegram_poller.lock")
TELEGRAM_CONTACTS_PATH = Path("data/telegram_contacts.json")
TELEGRAM_GENERATION_STATE_PATH = Path("data/telegram_digest_generation_state.json")
TELEGRAM_DELIVERY_STATE_PATH = Path("data/telegram_delivery_state.json")
SAVED_NEWS_PATH = Path("data/saved_news.json")
WEB_ASSET_ROOT = Path(__file__).with_name("web")
WEB_INDEX_PATH = WEB_ASSET_ROOT / "index.html"
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SESSION_COOKIE_NAME = "research_digest_session"
TELEGRAM_MESSAGE_LIMIT = 4096
ASSISTANT_MESSAGE_LIMIT = 4_000
ASSISTANT_CONTEXT_LIMIT = 16_000
ASSISTANT_HISTORY_LIMIT = 12
ASSISTANT_ATTACHMENT_MAX_BYTES = 8 * 1024 * 1024
# Base64 inflates payload size by ~4/3; add headroom for the rest of the JSON body.
ASSISTANT_ATTACHMENT_PAYLOAD_LIMIT = int(ASSISTANT_ATTACHMENT_MAX_BYTES * 1.4) + 96_000
ASSISTANT_ATTACHMENT_ALLOWED_MIME_TYPES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "application/pdf",
    "text/plain",
}
ASSISTANT_ATTACHMENT_ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".pdf", ".txt"}
# Voice messages: kept as a separate, smaller allowance (typical browser recordings are
# short clips) from generic file attachments above.
ASSISTANT_AUDIO_MAX_BYTES = 15 * 1024 * 1024
# Base64 inflates payload size by ~4/3; add headroom for the rest of the JSON body.
ASSISTANT_AUDIO_PAYLOAD_LIMIT = int(ASSISTANT_AUDIO_MAX_BYTES * 1.4) + 32_000
ASSISTANT_AUDIO_ALLOWED_MIME_TYPES = {
    "audio/webm",
    "audio/ogg",
    "audio/wav",
    "audio/x-wav",
    "audio/mp4",
    "audio/mp4a-latm",
    "audio/mpeg",
    "audio/m4a",
    "audio/aac",
}
ASSISTANT_AUDIO_ALLOWED_EXTENSIONS = {".webm", ".ogg", ".wav", ".mp4", ".m4a", ".mp3", ".aac"}
ASSISTANT_AUDIO_EXTENSION_TO_MIME = {
    ".webm": "audio/webm",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".mp4": "audio/mp4",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".aac": "audio/aac",
}
ASSISTANT_COMPARISON_PATTERN = re.compile(
    r"\b(?:confronta|confrontare|confronto|compara|comparare|paragona|paragonare|paragone|"
    r"differenze?|rispetto\s+(?:a|al|alla|alle|agli)|entrambe|due\s+notizie)\b",
    re.IGNORECASE,
)
TELEGRAM_POLL_INTERVAL_SECONDS = 15
TELEGRAM_SEND_MAX_RETRIES = 3
TELEGRAM_GENERATION_RETRY_SECONDS = 15 * 60
TELEGRAM_BROADCAST_DELAY_SECONDS = 0.04
NEWSLETTER_SUBJECT = "𝗟𝗲 𝗽𝗿𝗶𝗻𝗰𝗶𝗽𝗮𝗹𝗶 𝗻𝗼𝘃𝗶𝘁à 𝗔𝗜 𝗱𝗲𝗹𝗹𝗮 𝘀𝗲𝘁𝘁𝗶𝗺𝗮𝗻𝗮"
_TELEGRAM_POLLER_STARTED = False
_TELEGRAM_UPDATES_LOCK = threading.Lock()
_TELEGRAM_CONTACTS_LOCK = threading.RLock()
_TELEGRAM_DIGEST_GENERATION_LOCK = threading.Lock()
_TELEGRAM_DELIVERY_LOCK = threading.Lock()
_NEWSLETTER_DELIVERY_LOCK = threading.Lock()
_SAVED_NEWS_LOCK = threading.RLock()
_SUBSCRIBERS_LOCK = threading.RLock()
logger = logging.getLogger(__name__)

# Firestore remote-store state (set by _init_firestore_stores).
_FIRESTORE_ENABLED = False
_FIRESTORE_STORES: dict | None = None


def _init_firestore_stores() -> None:
    """Initialise Firestore persistence when configured, falling back to local files."""
    global _FIRESTORE_ENABLED, _FIRESTORE_STORES
    try:
        from src.firestore_store import create_firestore_store
        from src.config import load_config

        config = load_config(os.getenv("RESEARCH_DIGEST_CONFIG_PATH", "config.yaml"))
    except Exception:
        return

    stores = create_firestore_store(config)
    if stores is None:
        return

    _FIRESTORE_ENABLED = True
    _FIRESTORE_STORES = stores

    # Inject Firestore auth store into the auth module
    import src.auth as auth_mod

    auth_mod.set_auth_store(stores["auth"])
    logger.info(
        "Firestore stores attivi: auth, subscriber, telegram, saved_news, "
        "notification, fcm_token"
    )


@dataclass
class DigestView:
    path: str | None
    title: str
    subtitle: str
    warning: str | None
    entries: list[dict]
    footer: list[str]
    digest_date: str | None = None


@dataclass
class DigestHistoryItem:
    id: str
    path: str
    title: str
    subtitle: str
    updated: str
    key_points: list[str] | None = None
    entry_count: int = 0
    active: bool = False
    digest_date: str | None = None


@dataclass
class NewsletterResult:
    ok: bool
    title: str
    message: str
    email: str = ""
    delivered: bool = False


@dataclass(frozen=True)
class TelegramStart:
    """Validated private-chat /start event received from Telegram."""

    chat_id: str
    telegram_user_id: str
    payload: str | None
    username: str = ""
    first_name: str = ""
    last_name: str = ""


class TelegramDeliveryError(RuntimeError):
    """A Telegram delivery failure with retry and recipient-state metadata."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        blocked: bool = False,
        retry_after: int | None = None,
        sent_chunks: int = 0,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.blocked = blocked
        self.retry_after = retry_after
        self.sent_chunks = sent_chunks


class TelegramPollingConflictError(RuntimeError):
    """Raised when a different process is already polling this bot."""


def latest_digest_path(digest_dirs: tuple[str, ...] = DEFAULT_DIGEST_DIRS) -> Path | None:
    files = _collect_digest_paths(digest_dirs)
    if not files:
        return None
    return max(files, key=lambda path: path.stat().st_mtime)


def digest_history(digest_dirs: tuple[str, ...] = DEFAULT_DIGEST_DIRS) -> list[DigestHistoryItem]:
    grouped: dict[str, list[tuple[Path, DigestView]]] = {}
    ungrouped: list[tuple[Path, DigestView]] = []
    for path in _collect_digest_paths(digest_dirs):
        try:
            markdown = path.read_text(encoding="utf-8")
            view = parse_digest_markdown(markdown, path)
        except OSError:
            continue
        digest_date = _digest_date_key(view, path)
        if digest_date:
            grouped.setdefault(digest_date, []).append((path, view))
        else:
            ungrouped.append((path, view))

    dated_history: list[tuple[float, DigestHistoryItem]] = []
    for digest_date, records in grouped.items():
        records = sorted(records, key=lambda record: _digest_sort_timestamp(record[1], record[0]))
        latest_path = records[-1][0]
        latest_view = records[-1][1]
        updated_at = _digest_execution_datetime(latest_view, latest_path)
        merged_view = _merge_digest_views(
            [view for _, view in records],
            source_path=latest_path,
            digest_date=digest_date,
        )
        dated_history.append(
            (
                _timestamp_value(updated_at),
                DigestHistoryItem(
                    id=f"date:{digest_date}",
                    path=str(latest_path),
                    title=merged_view.title,
                    subtitle=merged_view.subtitle,
                    updated=_format_history_updated(updated_at),
                    key_points=_history_key_points(merged_view),
                    entry_count=len(merged_view.entries),
                    digest_date=digest_date,
                ),
            )
        )

    for path, view in ungrouped:
        updated_at = _digest_execution_datetime(view, path)
        dated_history.append(
            (
                _timestamp_value(updated_at),
                DigestHistoryItem(
                    id=_digest_id(path),
                    path=str(path),
                    title=view.title,
                    subtitle=view.subtitle,
                    updated=_format_history_updated(updated_at),
                    key_points=_history_key_points(view),
                    entry_count=len(view.entries),
                ),
            )
        )
    return [item for _, item in sorted(dated_history, key=lambda item: item[0], reverse=True)]


def load_digest_by_id(digest_id: str) -> DigestView | None:
    if digest_id.startswith("date:"):
        return load_digest_by_date(digest_id.removeprefix("date:"))
    if digest_id.startswith("week:"):
        item = next((item for item in weekly_digest_history() if item.id == digest_id), None)
        return load_digest_week_by_date(str(item.digest_date)) if item and item.digest_date else None
    for item in digest_history():
        if item.id == digest_id:
            path = Path(item.path)
            return parse_digest_markdown(path.read_text(encoding="utf-8"), path)
    legacy_path = _legacy_digest_path(digest_id)
    if legacy_path:
        return parse_digest_markdown(legacy_path.read_text(encoding="utf-8"), legacy_path)
    return None


def load_digest_by_date(
    digest_date: str,
    digest_dirs: tuple[str, ...] = DEFAULT_DIGEST_DIRS,
) -> DigestView | None:
    records: list[tuple[Path, DigestView]] = []
    for path in _collect_digest_paths(digest_dirs):
        try:
            view = parse_digest_markdown(path.read_text(encoding="utf-8"), path)
        except OSError:
            continue
        if _digest_date_key(view, path) == digest_date:
            records.append((path, view))
    if not records:
        return None
    records = sorted(records, key=lambda record: _digest_sort_timestamp(record[1], record[0]))
    return _merge_digest_views(
        [view for _, view in records],
        source_path=records[-1][0],
        digest_date=digest_date,
    )


def _history_week_key(item: DigestHistoryItem) -> tuple[int, int] | None:
    digest_date = _parse_iso_date(str(item.digest_date or ""))
    if digest_date is None:
        return None
    iso_year, iso_week, _ = digest_date.isocalendar()
    return iso_year, iso_week


def _iso_week_bounds(digest_date: str) -> tuple[date, date] | None:
    """Return the (Monday, Sunday) boundaries of the ISO week containing ``digest_date``.

    This is the single source of truth for "which week does this digest
    belong to": both the homepage period label and the history accordion
    derive their displayed range from it, so the two can never diverge
    again like they did when the homepage computed a rolling 7-day window
    from the latest run's date instead of the actual ISO week.
    """

    parsed = _parse_iso_date(digest_date)
    if parsed is None:
        return None
    monday = parsed - timedelta(days=parsed.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


def _iso_week_bounds_payload(digest_date: str) -> dict:
    bounds = _iso_week_bounds(digest_date)
    if bounds is None:
        return {"week_start": "", "week_end": ""}
    monday, sunday = bounds
    return {"week_start": monday.isoformat(), "week_end": sunday.isoformat()}


def _load_digest_week_from_items(items: list[DigestHistoryItem]) -> DigestView | None:
    dated_items = [item for item in items if _parse_iso_date(str(item.digest_date or "")) is not None]
    dated_items.sort(key=lambda item: str(item.digest_date))
    views = []
    for item in dated_items:
        view = load_digest_by_date(str(item.digest_date))
        if view is not None:
            views.append(view)
    if not views or not dated_items:
        return None
    latest = dated_items[-1]
    return _merge_digest_views(
        views,
        source_path=Path(latest.path) if latest.path else None,
        digest_date=str(latest.digest_date),
    )


def weekly_digest_history() -> list[DigestHistoryItem]:
    """Group every published run into one entry per ISO publication week."""

    grouped: dict[tuple[int, int], list[DigestHistoryItem]] = {}
    for item in digest_history():
        week_key = _history_week_key(item)
        if week_key is not None:
            grouped.setdefault(week_key, []).append(item)

    weekly_items = []
    for (iso_year, iso_week), items in grouped.items():
        items.sort(key=lambda item: str(item.digest_date))
        latest = items[-1]
        view = _load_digest_week_from_items(items)
        weekly_items.append(
            DigestHistoryItem(
                id=f"week:{iso_year}-W{iso_week:02d}",
                path=latest.path,
                title=view.title if view else latest.title,
                subtitle=view.subtitle if view else latest.subtitle,
                updated=latest.updated,
                key_points=_history_key_points(view) if view else (latest.key_points or []),
                entry_count=len(view.entries) if view else sum(item.entry_count for item in items),
                digest_date=latest.digest_date,
            )
        )
    return sorted(weekly_items, key=lambda item: str(item.digest_date or ""), reverse=True)


def load_digest_week_by_date(digest_date: str) -> DigestView | None:
    parsed_date = _parse_iso_date(digest_date)
    if parsed_date is None:
        return None
    iso_year, iso_week, _ = parsed_date.isocalendar()
    matching = [
        item
        for item in digest_history()
        if _history_week_key(item) == (iso_year, iso_week)
    ]
    return _load_digest_week_from_items(matching) or load_digest_by_date(digest_date)


def parse_digest_markdown(markdown: str, source_path: Path | None = None) -> DigestView:
    lines = markdown.splitlines()
    title = _first_line(lines, "# ", "Research Digest")
    subtitle = _first_line(lines, "*Beat:", "").strip("*")
    warning = _first_line(lines, "> ", None)
    footer = [line.strip("*") for line in lines if _is_footer_line(line)]
    entries: list[dict] = []
    current: dict | None = None
    active_field: str | None = None

    for line in lines:
        if line.startswith("## "):
            if current:
                entries.append(_clean_entry(current))
            current = {
                "title": line.removeprefix("## ").split(". ", 1)[-1],
                "meta": "",
                "url": "",
                "summary": "",
                "why": "",
            }
            active_field = None
            continue
        if current is None:
            continue
        if line.startswith("---") or _is_footer_line(line):
            active_field = None
            continue

        label, value = _split_markdown_label(line)
        if label == "fonte":
            current["meta"] = _strip_markdown_bold(line)
            active_field = None
        elif label == "url":
            current["url"] = _extract_url(value)
            active_field = None
        elif label == "sintesi":
            current["summary"] = value
            active_field = "summary"
        elif label in {"perche conta", "perché conta", "perchã© conta"}:
            current["why"] = value
            active_field = "why"
        elif active_field and line:
            current[active_field] = f"{current[active_field]} {line.strip()}".strip()

    if current:
        entries.append(_clean_entry(current))

    return DigestView(
        path=str(source_path) if source_path else None,
        title=title,
        subtitle=subtitle,
        warning=warning.removeprefix("> ").strip() if warning else None,
        entries=entries,
        footer=footer,
        digest_date=_digest_date_from_title(title) or _digest_date_from_path(source_path),
    )


def load_latest_digest() -> DigestView:
    history = digest_history()
    if not history:
        return _empty_digest_view(
            "Nessun digest trovato",
            "Esegui python main.py --demo per generare un digest offline.",
        )
    latest_item = history[0]
    if latest_item.digest_date:
        view = load_digest_by_date(latest_item.digest_date)
        if view:
            return view
    return load_digest_by_id(latest_item.id) or _empty_digest_view("Nessun digest trovato", "")


def load_current_digest() -> DigestView:
    # The newest published weekly snapshot remains on the homepage until a
    # newer one exists; publication, not the calendar rollover, archives it.
    history = weekly_digest_history()
    if not history:
        return _empty_digest_view(
            "Nessun digest trovato",
            "Esegui python main.py --demo per generare un digest offline.",
        )
    latest = history[0]
    return load_digest_week_by_date(str(latest.digest_date or "")) or load_latest_digest()


def _sidebar_requested_section(current_path: str) -> str:
    """Return the sidebar section explicitly selected in the current URL."""

    try:
        query = parse_qs(urlparse(str(current_path or "/")).query)
    except ValueError:
        return ""
    return str((query.get("sidebar") or [""])[0]).strip().lower()


def _sidebar_is_open(current_path: str) -> bool:
    """Return whether a navigation click requested the expanded sidebar."""

    return _sidebar_requested_section(current_path) in {"open", "saved"}


def render_html(
    view: DigestView,
    history: list[DigestHistoryItem] | None = None,
    user: dict | None = None,
    history_search_records: list[dict] | None = None,
    *,
    saved_sidebar_content: str = "",
    show_history_sidebar: bool = False,
    back_href: str | None = None,
    page_title: str | None = None,
    main_content: str | None = None,
    page_class: str = "",
    show_refresh: bool = True,
    current_path: str = "/",
    active_section: str | None = None,
) -> str:
    """Render the common digest shell and optional news-focused page content."""

    saved_ids = saved_news_ids_for_user(user)
    entries = "\n".join(
        _render_entry(entry, user=user, saved_ids=saved_ids, next_path=current_path)
        for entry in view.entries
    )
    if not view.entries:
        entries = _render_empty_entry()
    warning = f'<p class="warning">{html.escape(view.warning)}</p>' if view.warning else ""
    default_content = f'''{warning}
    <section class="grid">{entries}</section>'''
    content = main_content if main_content is not None else default_content
    footer = " ".join(html.escape(item) for item in view.footer)
    source = html.escape(view.path or "")
    display_title = page_title or _display_digest_title(view.title)
    refresh_href = "/today"
    refresh_control = f'<a class="button primary" href="{refresh_href}">Aggiorna</a>' if show_refresh else ""
    back_control = (
        f'''<a class="button back-button" href="{html.escape(_safe_next_path(back_href), quote=True)}"
      aria-label="Torna alla pagina principale"><span class="back-button-arrow" aria-hidden="true">&#8592;</span> Indietro</a>'''
        if back_href
        else ""
    )
    history_content = ""
    if show_history_sidebar or active_section == "history":
        history_content = _render_history_panel(history or [], history_search_records or [])
    body_classes = " ".join(
        value for value in (page_class, "sidebar-open" if _sidebar_is_open(current_path) else "") if value
    )
    return f"""<!doctype html>
<html lang="it">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="/assets/images/favicon.svg" type="image/svg+xml">
  <title>{html.escape(display_title)}</title>
  <link rel="stylesheet" href="/assets/css/styles.css">
  <style>
    :root {{
      color-scheme: light;
      --ink: #17202a;
      --muted: #5f6f7c;
      --line: #d7dee5;
      --paper: #ffffff;
      --bg: #f4f7f9;
      --accent: #176b87;
      --accent-soft: #d9eef4;
      --shade: rgba(23, 32, 42, 0.35);
    }}
    * {{ box-sizing: border-box; }}
    html, body {{
      margin: 0;
      padding: 0;
      min-height: 100%;
      font-family: var(--font-sans, "Segoe UI", Arial, sans-serif);
      background: #d8e0ee;
      color: var(--ink);
      text-rendering: optimizeLegibility;
      -webkit-font-smoothing: antialiased;
      -moz-osx-font-smoothing: grayscale;
    }}
    #canvas-container {{
      position: relative;
      width: 100%;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: flex-start;
      padding: calc(160px - 1cm) 24px 240px;
      box-sizing: border-box;
      background: radial-gradient(ellipse at center 35%, #f0f3f8 0%, #e1e6ef 25%, #d3dbe8 55%, #b3c1d7 100%);
      overflow: hidden;
    }}
    .page-header {{
      border-bottom: 1px solid var(--line);
      background: var(--paper);
      padding: 24px clamp(16px, 4vw, 48px) 24px clamp(64px, 8vw, 96px);
      position: relative;
      width: 100%;
      max-width: 1080px;
    }}
    .content {{
      width: min(1080px, calc(100% - 32px));
      margin: 24px auto 48px;
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: clamp(26px, 4vw, 42px);
      letter-spacing: 0;
    }}
    .subtitle, .source, footer {{
      color: var(--muted);
      line-height: 1.5;
    }}
    .toolbar {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 16px;
    }}
    .button {{
      border: 1px solid var(--line);
      background: var(--paper);
      color: var(--ink);
      border-radius: 6px;
      padding: 9px 12px;
      text-decoration: none;
      font-weight: 600;
    }}
    .button.primary {{
      background: var(--accent);
      border-color: var(--accent);
      color: white;
    }}
    .back-button {{
      display: inline-flex;
      align-items: center;
      gap: 7px;
      border-color: var(--accent);
      color: var(--accent);
    }}
    .back-button:hover,
    .back-button:focus-visible {{
      background: var(--accent-soft);
      outline: 2px solid transparent;
    }}
    .back-button-arrow {{
      font-size: 20px;
      line-height: 1;
    }}
    .warning {{
      background: #fff4d6;
      border: 1px solid #ead28d;
      border-radius: 6px;
      padding: 12px 14px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 16px;
    }}
    article {{
      background: var(--paper);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
      min-height: 260px;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }}
    article h2 {{
      margin: 0;
      font-size: 20px;
      line-height: 1.35;
      letter-spacing: 0;
      text-wrap: pretty;
    }}
    .entry-heading {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
    }}
    .entry-heading h2 {{ flex: 1; }}
    .entry-action {{
      flex: 0 0 auto;
      display: inline-grid;
      place-items: center;
      width: 34px;
      height: 34px;
      padding: 0;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--paper);
      color: var(--accent);
      font-size: 17px;
      text-decoration: none;
      cursor: pointer;
    }}
    .entry-action:hover, .entry-action:focus-visible {{
      background: var(--accent-soft);
      outline: 2px solid transparent;
    }}
    .bookmark-action.is-saved {{
      background: var(--accent);
      border-color: var(--accent);
      color: white;
    }}
    .meta {{
      color: var(--muted);
      font-size: 14px;
      line-height: 1.6;
    }}
    .label {{
      width: fit-content;
      padding: 4px 8px;
      border-radius: 6px;
      background: var(--accent-soft);
      color: var(--accent);
      font-size: 13px;
      font-weight: 700;
    }}
    .label-placeholder {{
      width: 56px;
      height: 24px;
      visibility: hidden;
    }}
    p {{
      margin: 0;
      line-height: 1.68;
      text-wrap: pretty;
    }}
    .link {{
      margin-top: auto;
      overflow-wrap: anywhere;
      color: var(--accent);
      font-weight: 700;
    }}
    .entry-link-row {{
      display: flex;
      align-items: center;
      gap: 10px;
      margin-top: auto;
    }}
    .entry-link-row .link {{
      flex: 1;
      margin-top: 0;
    }}
    .single-news-grid {{
      grid-template-columns: minmax(0, 640px);
      justify-content: center;
    }}
    .saved-news-detail-page .single-news-grid > article {{
      width: 388px;
      max-width: 100%;
      height: 801.383px;
      min-height: 0;
      overflow-y: auto;
      justify-self: center;
    }}
    .toast {{
      position: fixed;
      right: 18px;
      bottom: 18px;
      z-index: 20;
      max-width: min(360px, calc(100vw - 36px));
      padding: 12px 14px;
      border-radius: 6px;
      background: var(--ink);
      color: white;
      box-shadow: 0 8px 24px rgba(23, 32, 42, 0.24);
      opacity: 0;
      transform: translateY(8px);
      pointer-events: none;
      transition: opacity 160ms ease, transform 160ms ease;
    }}
    .toast.visible {{ opacity: 1; transform: translateY(0); }}
    .visually-hidden {{
      position: absolute;
      width: 1px;
      height: 1px;
      padding: 0;
      margin: -1px;
      overflow: hidden;
      clip: rect(0, 0, 0, 0);
      white-space: nowrap;
      border: 0;
    }}
    footer {{
      width: min(1080px, calc(100% - 32px));
      max-width: 1080px;
      margin: 0 auto 32px;
    }}
    @media (max-width: 640px) {{
      .page-header {{
        padding-left: 72px;
      }}
      .content, footer {{
        width: calc(100% - 86px);
        margin-left: 70px;
        margin-right: 16px;
      }}
    }}
    {render_sidebar_css()}
  </style>
</head>
<body class="{html.escape(body_classes, quote=True)}">
  {render_sidebar(active_section, history_content, saved_sidebar_content)}
  <main id="canvas-container">
    <header class="brand-header">
      <div class="brand-lockup" role="img" aria-label="Nuvia.ai">
        <span class="brand-wordmark" aria-hidden="true">
          <span class="brand-initial">N</span>
          <span class="brand-tail">uvia.ai</span>
        </span>

        <svg class="brand-swoosh" viewBox="0 0 760 120" aria-hidden="true">
          <defs>
            <linearGradient id="brand-swoosh-shadow-gradient" x1="0" y1="0" x2="1" y2="0">
              <stop offset="0" stop-color="#91a4c8" stop-opacity="0"></stop>
              <stop offset="0.16" stop-color="#91a4c8" stop-opacity="0.62"></stop>
              <stop offset="0.72" stop-color="#91a4c8" stop-opacity="0.42"></stop>
              <stop offset="1" stop-color="#91a4c8" stop-opacity="0"></stop>
            </linearGradient>
            <linearGradient id="brand-swoosh-light-gradient" x1="0" y1="0" x2="1" y2="0">
              <stop offset="0" stop-color="#ffffff" stop-opacity="0"></stop>
              <stop offset="0.12" stop-color="#ffffff" stop-opacity="0.96"></stop>
              <stop offset="0.68" stop-color="#edf3fc" stop-opacity="0.94"></stop>
              <stop offset="1" stop-color="#dce6f7" stop-opacity="0"></stop>
            </linearGradient>
          </defs>
          <path
            class="brand-swoosh-shadow"
            d="M18 104 C198 16 480 12 742 76"
            transform="translate(0 5)"
          ></path>
          <path class="brand-swoosh-stroke" d="M18 104 C198 16 480 12 742 76"></path>
          <path class="brand-swoosh-highlight" d="M18 102 C198 14 480 10 742 74"></path>
        </svg>
      </div>
    </header>
    <header class="page-header">
      <h1>{html.escape(display_title)}</h1>
      <div class="subtitle">{html.escape(view.subtitle)}</div>
      <div class="source">{source}</div>
      <nav class="toolbar">
        {back_control}
        {refresh_control}
      </nav>
    </header>
    <div class="content">
      {content}
    </div>
    <footer>{footer}</footer>
    <div class="toast" id="news-toast" role="status" aria-live="polite"></div>
    {_render_news_actions_script()}
  </main>
</body>
</html>"""


def _entry_from_news_record(record: dict) -> dict:
    return {
        key: str(record.get(key) or "")
        for key in ("title", "meta", "url", "summary", "why")
    }


def _news_source_label(record: dict) -> str:
    meta = _display_meta_line(str(record.get("meta") or ""))
    match = re.search(r"(?:^|\*\*)Fonte:?\*?\*?\s*(.*?)(?=\s+-\s+(?:\*\*)?Data|$)", meta, re.IGNORECASE)
    if match:
        source = match.group(1).strip(" -*")
        if source:
            return source
    return meta or "Fonte non indicata"


def _news_detail_content(record: dict, user: dict | None, current_path: str) -> str:
    entry = _entry_from_news_record(record)
    saved_ids = saved_news_ids_for_user(user)
    return '<section class="grid single-news-grid">{}</section>'.format(
        _render_entry(
            entry,
            user=user,
            saved_ids=saved_ids,
            next_path=current_path,
            news_id=str(record.get("id") or ""),
        )
    )


def render_news_detail_html(
    record: dict,
    user: dict | None,
    *,
    current_path: str,
    shared: bool = False,
) -> str:
    """Render a centered copy of the regular home card for one news item."""

    entry = _entry_from_news_record(record)
    source = _news_source_label(record)
    view = DigestView(
        path=None,
        title="Notizia condivisa" if shared else "Notizia",
        subtitle=source,
        warning=None,
        entries=[entry],
        footer=[],
        digest_date=str(record.get("digest_date") or "") or None,
    )
    return render_html(
        view,
        user=user,
        page_title="Notizia condivisa" if shared else "Notizia",
        main_content=_news_detail_content(record, user, current_path),
        page_class="news-detail-page" if shared else "news-detail-page saved-news-detail-page",
        show_refresh=False,
        current_path=current_path,
        active_section="saved" if not shared else None,
        back_href="/",
    )


def _render_saved_sidebar_panel(records: list[dict]) -> str:
    items = []
    for record in records:
        news_id = str(record.get("id") or "")
        if not re.fullmatch(r"n_[a-f0-9]{24}", news_id):
            continue
        title = normalize_news_title(record.get("title") or "Senza titolo")
        date_label = _saved_news_date_label(record)
        aria_label = html.escape(f"Apri la notizia: {title}", quote=True)
        items.append(
            f'''<a class="saved-news-item" data-saved-news-item href="/news/{news_id}"
  target="_blank" rel="noopener noreferrer" aria-label="{aria_label}">
  <span class="saved-news-title">{html.escape(title)}</span>
  <span class="saved-news-date">{html.escape(date_label)}</span>
</a>'''
        )
    items_html = "\n".join(items) or (
        '<p class="saved-news-empty">Nessuna notizia salvata.</p>'
    )
    return f'''<div class="saved-news-panel" data-saved-news-panel>
  <div class="saved-news-list" aria-label="Notizie salvate">{items_html}</div>
</div>'''


def _saved_news_date_label(record: dict) -> str:
    raw_date = _meta_field(str(record.get("meta") or ""), "Data") or str(
        record.get("digest_date") or ""
    ).strip()
    parsed_date = _parse_iso_date(raw_date)
    return parsed_date.strftime("%d/%m/%Y") if parsed_date else raw_date or "Data non disponibile"


def _render_news_actions_script() -> str:
    return """<script>
(() => {
  const toast = document.getElementById("news-toast");
  let toastTimer;
  const notify = (message) => {
    if (!toast) return;
    toast.textContent = message;
    toast.classList.add("visible");
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => toast.classList.remove("visible"), 2600);
  };
  const copyText = async (value) => {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(value);
      return;
    }
    const field = document.createElement("textarea");
    field.value = value;
    field.setAttribute("readonly", "");
    field.style.position = "fixed";
    field.style.opacity = "0";
    document.body.appendChild(field);
    field.select();
    const copied = document.execCommand("copy");
    field.remove();
    if (!copied) throw new Error("copy-failed");
  };
  document.addEventListener("click", async (event) => {
    const share = event.target.closest(".share-action");
    if (share) {
      event.preventDefault();
      const path = share.dataset.sharePath || "";
      try {
        await copyText(new URL(path, window.location.origin).toString());
        notify("Link condivisibile copiato negli appunti.");
      } catch (_) {
        notify("Non riesco a copiare il link: puoi copiarlo dalla barra del browser.");
      }
      return;
    }
    const bookmark = event.target.closest("button.bookmark-action[data-news-id]");
    if (!bookmark) return;
    event.preventDefault();
    bookmark.disabled = true;
    const removing = bookmark.classList.contains("is-saved");
    try {
      const response = await fetch("/api/saved", {
        method: "POST",
        headers: {"Content-Type": "application/json", "Accept": "application/json"},
        body: JSON.stringify({news_id: bookmark.dataset.newsId, action: removing ? "remove" : "save"}),
      });
      if (response.status === 401) {
        window.location.assign("/login?next=" + encodeURIComponent(window.location.pathname + window.location.search));
        return;
      }
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(payload.message || "save-failed");
      const saved = Boolean(payload.saved);
      bookmark.classList.toggle("is-saved", saved);
      bookmark.setAttribute("aria-pressed", String(saved));
      const label = saved ? "Rimuovi dalle notizie salvate" : "Salva notizia";
      bookmark.setAttribute("aria-label", label);
      bookmark.setAttribute("title", label);
      notify(saved ? "Notizia salvata." : "Notizia rimossa dai salvati.");
    } catch (_) {
      notify("Non riesco ad aggiornare le notizie salvate. Riprova.");
    } finally {
      bookmark.disabled = false;
    }
  });
})();
</script>"""


def _render_history_panel(history: list[DigestHistoryItem], records: list[dict]) -> str:
    rendered_items = []
    for index, item in enumerate(history):
        news_records = _history_records_for_item(item, records)
        rendered_items.append(
            _render_history_item(
                item,
                index=index,
                news_records=news_records,
                **_history_search_metadata(item, news_records),
            )
        )
    item_count = len(history)
    record_count = len(records)
    news_label = "notizia" if record_count == 1 else "notizie"
    default_status = f"{record_count} {news_label} in {item_count} digest"
    items_html = "\n".join(rendered_items) or (
        '<p class="history-empty">Nessun digest archiviato al momento.</p>'
    )
    return f'''<div class="history-panel" data-history-panel>
  <label class="visually-hidden" for="history-search">Cerca nella cronologia</label>
  <input class="history-search" id="history-search" type="search" autocomplete="off"
    placeholder="Cerca per titolo, fonte o contenuto" list="history-suggestions">
  <datalist id="history-suggestions"></datalist>
  <p class="history-search-status" id="history-search-status" data-default-label="{html.escape(default_status, quote=True)}">{default_status}</p>
  <div class="history-list" id="history-list">{items_html}
    <p class="history-empty history-no-results" id="history-no-results" role="status" aria-live="polite" hidden>Nessuna notizia trovata.</p>
  </div>
</div>
{_render_history_search_script()}'''


def _history_records_for_item(item: DigestHistoryItem, records: list[dict]) -> list[dict]:
    return [
        record
        for record in records
        if (
            (
                item.digest_date
                and str(record.get("digest_date") or "") == item.digest_date
            )
            or (
                not item.digest_date
                and str(record.get("digest_title") or "") == item.title
            )
        )
    ]


def _history_search_metadata(item: DigestHistoryItem, records: list[dict]) -> dict:
    news_titles = [str(record.get("title") or "").strip() for record in records]
    sources = [_news_source_label(record) for record in records]
    visible_title = _display_digest_title(item.title, item.updated)
    search_values = _history_text_values(asdict(item))
    search_values.extend(_history_text_values(records))
    search_values.extend(sources)
    search_suggestions = list(
        dict.fromkeys(
            value
            for value in [*news_titles, *sources, visible_title]
            if value
        )
    )
    return {
        "search_text": " ".join(value for value in search_values if value),
        "search_title": " ".join(value for value in [visible_title, *news_titles] if value),
        "search_source": " ".join(value for value in sources if value) or item.subtitle,
        "search_suggestions": search_suggestions,
    }


def _history_text_values(value: object) -> list[str]:
    """Flatten textual history data so the client filter covers every real field."""

    if value is None:
        return []
    if isinstance(value, dict):
        return [text for nested in value.values() for text in _history_text_values(nested)]
    if isinstance(value, (list, tuple, set)):
        return [text for nested in value for text in _history_text_values(nested)]
    text = str(value).strip()
    return [text] if text else []


def _render_history_search_script() -> str:
    return """<script>
(() => {
  const input = document.getElementById("history-search");
  const list = document.getElementById("history-list");
  const suggestions = document.getElementById("history-suggestions");
  const status = document.getElementById("history-search-status");
  const noResults = document.getElementById("history-no-results");
  if (!input || !list || !suggestions || !status || !noResults) return;
  const cards = Array.from(list.querySelectorAll("[data-history-item]"));
  const defaultStatus = status.dataset.defaultLabel || status.textContent;
  const originals = new WeakMap();
  const openedBeforeSearch = new WeakMap(cards.map((card) => [card, card.open]));
  const highlightable = ".history-title, .history-meta, .history-source-title, .history-source-link";
  const normalize = (value) => String(value || "").normalize("NFD")
    .replace(/[\\u0300-\\u036f]/g, "").toLocaleLowerCase();
  const words = (value) => normalize(value).trim().split(/\\s+/).filter(Boolean);
  const reset = (card) => card.querySelectorAll(highlightable).forEach((node) => {
    if (originals.has(node)) node.innerHTML = originals.get(node);
    else originals.set(node, node.innerHTML);
  });
  const highlight = (card, query) => {
    const terms = words(query);
    if (!terms.length) return;
    card.querySelectorAll(highlightable).forEach((node) => {
      const source = node.textContent || "";
      const matcher = new RegExp("(" + terms.map((term) => term.replace(/[.*+?^${}()|[\\]\\\\]/g, "\\\\$&")).join("|") + ")", "ig");
      const pieces = source.split(matcher);
      if (pieces.length < 2) return;
      node.textContent = "";
      pieces.forEach((piece, index) => {
        if (!piece) return;
        if (index % 2) {
          const mark = document.createElement("mark");
          mark.className = "search-highlight";
          mark.textContent = piece;
          node.appendChild(mark);
        } else node.appendChild(document.createTextNode(piece));
      });
    });
  };
  const escapeRegExp = (value) => String(value || "").replace(/[\\^$.*+?()[\\]{}|]/g, "\\\\$&");
  const exactMatch = (value, phrase) => {
    if (!phrase) return false;
    const expression = escapeRegExp(phrase).replace(/\\s+/g, "\\\\s+");
    return new RegExp(
      "(^|[^\\\\p{L}\\\\p{N}])" + expression + "(?=$|[^\\\\p{L}\\\\p{N}])",
      "u",
    ).test(value);
  };
  const relevance = (card, query) => {
    const termList = words(query);
    if (!termList.length) return { exactMatch: 0, exactField: 0, score: 0 };
    const title = normalize(card.dataset.title);
    const source = normalize(card.dataset.source);
    const content = normalize(card.dataset.search);
    const phrase = termList.join(" ");
    const exactField = exactMatch(title, phrase) ? 3
      : exactMatch(source, phrase) ? 2
      : exactMatch(content, phrase) ? 1
      : 0;
    let score = 0;
    if (title.includes(phrase)) score += 24;
    if (source.includes(phrase)) score += 15;
    if (content.includes(phrase)) score += 7;
    termList.forEach((term) => {
      if (title.startsWith(term)) score += 12;
      else if (title.includes(term)) score += 9;
      if (source.includes(term)) score += 6;
      if (content.includes(term)) score += 3;
    });
    return { exactMatch: Number(exactField > 0), exactField, score };
  };
  const matches = (card, query) => {
    const terms = words(query);
    const content = normalize(card.dataset.search);
    return terms.every((term) => content.includes(term));
  };
  const rank = (query) => cards.map((card) => ({
    card,
    index: Number(card.dataset.index),
    ...relevance(card, query),
  }))
    .filter((item) => !query || matches(item.card, query))
    .sort((left, right) => right.exactMatch - left.exactMatch
      || right.exactField - left.exactField
      || right.score - left.score
      || left.index - right.index);
  const suggestionsFor = (card) => {
    try {
      const values = JSON.parse(card.dataset.suggestions || "[]");
      return Array.isArray(values) ? values : [];
    } catch (_) {
      return [];
    }
  };
  const updateSuggestions = (query, ranked) => {
    suggestions.replaceChildren();
    if (!query.trim()) return;
    const needle = normalize(query);
    const values = [];
    const seen = new Set();
    const add = (value, related = false) => {
      const text = String(value || "").trim();
      const key = normalize(text);
      if (!text || seen.has(key)) return;
      seen.add(key);
      values.push({text, related});
    };
    ranked.forEach(({card}) => {
      suggestionsFor(card).forEach((value) => {
        if (normalize(value).includes(needle)) add(value);
      });
    });
    ranked.forEach(({card}) => {
      const value = suggestionsFor(card)[0] || card.dataset.title || card.dataset.source;
      add(value, true);
    });
    values.slice(0, 6).forEach(({text, related}) => {
      const option = document.createElement("option");
      option.value = text;
      if (related) option.label = `Risultato correlato: ${text}`;
      suggestions.appendChild(option);
    });
  };
  const search = () => {
    const query = input.value.trim();
    const ranked = rank(query);
    updateSuggestions(query, ranked);
    cards.forEach((card) => { reset(card); card.hidden = true; });
    ranked.forEach((item) => {
      item.card.hidden = false;
      item.card.open = query ? true : Boolean(openedBeforeSearch.get(item.card));
      highlight(item.card, query);
      list.insertBefore(item.card, noResults);
    });
    noResults.hidden = !query || ranked.length > 0;
    status.textContent = !query
      ? defaultStatus
      : ranked.length
        ? `${ranked.length} risultat${ranked.length === 1 ? "o" : "i"} per \"${query}\"`
        : "Nessuna notizia trovata.";
  };
  cards.forEach((card) => card.addEventListener("toggle", () => {
    if (!input.value.trim()) openedBeforeSearch.set(card, card.open);
  }));
  input.addEventListener("input", search);
  input.addEventListener("search", search);
})();
</script>"""


def _json_href_for_view(view: DigestView) -> str:
    if view.digest_date:
        return f"/api/latest?date={quote(view.digest_date, safe='')}"
    if view.path:
        return f"/api/latest?id={quote(_digest_id(Path(view.path)), safe='')}"
    return "/api/latest"


def _safe_next_path(value: str | None) -> str:
    """Keep post-auth redirects on this server and reject malformed locations."""

    candidate = str(value or "").strip()
    parsed = urlparse(candidate)
    if (
        not candidate.startswith("/")
        or candidate.startswith("//")
        or "\\" in candidate
        or "\r" in candidate
        or "\n" in candidate
        or parsed.scheme
        or parsed.netloc
    ):
        return "/"
    if parsed.path in {"/login", "/register", "/logout"}:
        return "/"
    return candidate


def _form_value(fields: dict[str, list[str]], name: str, *, strip: bool = True) -> str:
    value = str((fields.get(name) or [""])[0])
    return value.strip() if strip else value


def _form_checked(fields: dict[str, list[str]], name: str) -> bool:
    value = _form_value(fields, name).casefold()
    return value in {"1", "true", "on", "yes"}


def render_auth_html(
    mode: str = "login",
    result: auth.AuthResult | None = None,
    values: dict[str, str] | None = None,
    next_path: str = "/",
) -> str:
    """Render the account entry page without exposing account details in URLs."""

    mode = "register" if mode == "register" else "login"
    values = values or {}
    safe_next = _safe_next_path(next_path)
    encoded_next = quote(safe_next, safe="")
    status = ""
    if result:
        status_class = "success" if result.ok else "error"
        status = f"""<section class="status {status_class}" role="alert">
      {html.escape(result.message)}
    </section>"""
    login_active = " active" if mode == "login" else ""
    register_active = " active" if mode == "register" else ""
    email_value = html.escape(str(values.get("email") or ""))
    first_name = html.escape(str(values.get("first_name") or ""))
    last_name = html.escape(str(values.get("last_name") or ""))
    email_checked = " checked" if values.get("email_notifications", "1") != "0" else ""
    telegram_link_pair = _telegram_link_pair("onboarding")
    telegram_link = (
        _render_telegram_open_link(*telegram_link_pair)
        if telegram_link_pair
        else '<span class="telegram-link disabled" aria-disabled="true">Telegram non configurato</span>'
    )
    auth_form = (
        f"""<form method="post" action="/login" class="auth-form">
          <input type="hidden" name="next" value="{html.escape(safe_next)}">
          <label for="login-email">Email</label>
          <input id="login-email" name="email" type="email" required autocomplete="email" placeholder="nome@example.com" value="{email_value}">
          <label for="login-password">Password</label>
          <input id="login-password" name="password" type="password" required autocomplete="current-password">
          <label class="check-row" for="remember-me">
            <input id="remember-me" name="remember_me" type="checkbox" value="1">
            <span>Rimani connesso</span>
          </label>
          <button type="submit">Accedi</button>
        </form>"""
        if mode == "login"
        else f"""<form method="post" action="/register" class="auth-form">
          <input type="hidden" name="next" value="{html.escape(safe_next)}">
          <div class="name-row">
            <div>
              <label for="first-name">Nome</label>
              <input id="first-name" name="first_name" type="text" required maxlength="100" autocomplete="given-name" value="{first_name}">
            </div>
            <div>
              <label for="last-name">Cognome</label>
              <input id="last-name" name="last_name" type="text" required maxlength="100" autocomplete="family-name" value="{last_name}">
            </div>
          </div>
          <label for="register-email">Email</label>
          <input id="register-email" name="email" type="email" required autocomplete="email" placeholder="nome@example.com" value="{email_value}">
          <label for="register-password">Password</label>
          <input id="register-password" name="password" type="password" required minlength="8" autocomplete="new-password" aria-describedby="password-hint">
          <p class="hint" id="password-hint">Almeno 8 caratteri, con maiuscola, minuscola, numero e carattere speciale.</p>
          <label for="password-confirmation">Conferma password</label>
          <input id="password-confirmation" name="password_confirmation" type="password" required minlength="8" autocomplete="new-password">
          <fieldset class="channel-preferences">
            <legend>Ricezione notizie</legend>
            <p class="hint">Scegli i canali su cui desideri ricevere gli aggiornamenti.</p>
            <label class="check-row" for="notify-email">
              <input id="notify-email" name="email_notifications" type="checkbox" value="1"{email_checked}>
              <span>Email</span>
            </label>
            <p class="hint">Per ricevere il digest su Telegram, apri Telegram e premi <strong>Start</strong>.</p>
            {telegram_link}
          </fieldset>
          <button type="submit">Crea account</button>
        </form>"""
    )
    return f"""<!doctype html>
<html lang="it">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="/assets/images/favicon.svg" type="image/svg+xml">
  <title>{"Crea account" if mode == "register" else "Accedi"} · Research Digest</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #17202a;
      --muted: #5f6f7c;
      --line: #d7dee5;
      --paper: #ffffff;
      --bg: #f4f7f9;
      --accent: #176b87;
      --accent-soft: #d9eef4;
      --danger: #9b1c1c;
      --success: #116149;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      min-height: 100vh;
      margin: 0;
      display: grid;
      place-items: center;
      padding: 24px 16px;
      font-family: "Segoe UI", Arial, sans-serif;
      background: var(--bg);
      color: var(--ink);
    }}
    main {{ width: min(520px, 100%); }}
    .brand {{ margin: 0 0 8px; font-size: clamp(28px, 5vw, 40px); }}
    .subtitle {{ margin: 0 0 22px; color: var(--muted); line-height: 1.55; }}
    .panel {{ padding: 22px; border: 1px solid var(--line); border-radius: 8px; background: var(--paper); box-shadow: 0 12px 30px rgba(23, 32, 42, 0.08); }}
    .tabs {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 20px; }}
    .tab {{ display: grid; place-items: center; min-height: 42px; border: 1px solid var(--line); border-radius: 6px; color: var(--ink); font-weight: 700; text-decoration: none; }}
    .tab.active {{ border-color: var(--accent); background: var(--accent); color: white; }}
    .auth-form {{ display: grid; gap: 10px; }}
    label {{ font-weight: 700; }}
    input[type="email"], input[type="password"], input[type="text"] {{ width: 100%; min-height: 44px; border: 1px solid var(--line); border-radius: 6px; padding: 10px 12px; font: inherit; color: var(--ink); }}
    input:focus {{ outline: 3px solid var(--accent-soft); border-color: var(--accent); }}
    button {{ width: fit-content; min-height: 42px; margin-top: 4px; border: 1px solid var(--accent); border-radius: 6px; background: var(--accent); color: white; padding: 9px 14px; font: inherit; font-weight: 700; cursor: pointer; }}
    .check-row {{ display: flex; align-items: center; gap: 9px; width: fit-content; font-weight: 600; cursor: pointer; }}
    .check-row input {{ width: 17px; height: 17px; accent-color: var(--accent); }}
    .name-row {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }}
    .name-row div {{ display: grid; gap: 10px; }}
    .channel-preferences {{ display: grid; gap: 10px; margin: 8px 0 0; padding: 14px; border: 1px solid var(--line); border-radius: 6px; }}
    .channel-preferences legend {{ padding: 0 5px; font-weight: 800; }}
    .hint {{ margin: 0; color: var(--muted); font-size: 14px; line-height: 1.5; }}
    .telegram-launch {{ display: grid; gap: 6px; width: fit-content; }}
    .telegram-link {{ display: inline-flex; align-items: center; min-height: 44px; width: fit-content; border: 1px solid var(--accent); border-radius: 6px; padding: 9px 14px; color: var(--accent); font-weight: 700; text-decoration: none; }}
    .telegram-link:hover {{ background: var(--accent-soft); }}
    .telegram-link:focus-visible {{ outline: 3px solid var(--accent-soft); outline-offset: 2px; }}
    .telegram-link.disabled {{ border-color: var(--line); color: var(--muted); cursor: not-allowed; }}
    .telegram-launch-status {{ min-height: 18px; color: var(--muted); font-size: 13px; line-height: 1.35; }}
    .status {{ margin-bottom: 16px; border-left: 5px solid var(--accent); border-radius: 6px; background: var(--paper); padding: 12px 14px; line-height: 1.5; }}
    .status.error {{ border-left-color: var(--danger); }}
    .status.success {{ border-left-color: var(--success); }}
    @media (max-width: 440px) {{ .name-row {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <main>
    <h1 class="brand">Research Digest</h1>
    <p class="subtitle">Accedi per ritrovare le tue preferenze e le notizie salvate su ogni dispositivo.</p>
    {status}
    <section class="panel">
      <nav class="tabs" aria-label="Autenticazione">
        <a class="tab{login_active}" href="/login?next={encoded_next}">Accedi</a>
        <a class="tab{register_active}" href="/register?next={encoded_next}">Registrati</a>
      </nav>
      {auth_form}
    </section>
  </main>
  {_render_telegram_open_script()}
</body>
</html>"""


def render_settings_html(
    result: NewsletterResult | auth.AuthResult | None = None,
    email_value: str = "",
    user: dict | None = None,
    *,
    current_path: str = "/settings",
) -> str:
    user = user or {}
    stored_email = str(user.get("email") or "")
    email_value = email_value or stored_email
    preferences = user.get("preferences") or {}
    email_notifications_checked = " checked" if preferences.get("email", True) else ""
    status = ""
    if result:
        status_class = "success" if result.ok else "error"
        status_title = getattr(
            result,
            "title",
            "Impostazioni aggiornate" if result.ok else "Aggiornamento non riuscito",
        )
        status = f"""<section class="status {status_class}" role="status">
      <strong>{html.escape(status_title)}</strong>
      <p>{html.escape(result.message)}</p>
    </section>"""
    smtp_ready = _smtp_configured()
    smtp_text = (
        "Invio email attivo: il server SMTP \u00e8 configurato."
        if smtp_ready
        else "SMTP non configurato: la richiesta viene salvata in outbox locale finch\u00e9 non imposti SMTP_HOST e SMTP_FROM."
    )
    telegram_link_pair = _telegram_account_link_pair(user)
    telegram_link = (
        _render_telegram_open_link(*telegram_link_pair, status_url="/api/telegram/status")
        if telegram_link_pair
        else '<span class="telegram-link disabled" aria-disabled="true">Telegram non configurato</span>'
    )
    push_config = public_firebase_config()
    push_text = (
        "Firebase \u00e8 configurato: puoi abilitare le notifiche push da questo browser."
        if push_config.get("configured")
        else "Firebase non \u00e8 ancora configurato o \u00e8 disattivato: la sezione resta pronta per il deploy."
    )
    suspension_section = _render_notification_suspension_section()
    suspension_script = _render_notification_suspension_script()
    push_script = _render_push_notification_script()
    sidebar_open = _sidebar_is_open(current_path)
    sidebar_classes = "sidebar-open" if sidebar_open else ""
    sidebar_markup = render_sidebar("settings") if user else ""
    account_section = ""
    if user:
        account_section = f"""<section>
      <h2>Profilo</h2>
      <form method="post" action="/settings/profile">
        <label for="account-email">Email</label>
        <input id="account-email" name="email" type="email" required autocomplete="email" placeholder="nome@example.com" value="{html.escape(stored_email)}">
        <p class="hint">L'indirizzo resta associato al tuo account e puoi modificarlo in qualsiasi momento.</p>
        <button type="submit">Salva email</button>
      </form>
    </section>
    <section>
      <h2>Canali di notifica</h2>
      <form method="post" action="/settings/preferences">
        <label class="check-row" for="settings-email-notifications">
          <input id="settings-email-notifications" name="email_notifications" type="checkbox" value="1"{email_notifications_checked}>
          <span>Email</span>
        </label>
        <button type="submit">Salva preferenze</button>
      </form>
    </section>"""
    logout_section = ""
    if user:
        logout_section = """<section class="account-actions">
      <h2>Account</h2>
      <p class="hint">Termina la sessione su questo dispositivo.</p>
      <form method="post" action="/logout">
        <button type="submit" class="danger">Logout</button>
      </form>
    </section>"""
    return f"""<!doctype html>
<html lang="it">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="/assets/images/favicon.svg" type="image/svg+xml">
  <title>Impostazioni newsletter</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #17202a;
      --muted: #5f6f7c;
      --line: #d7dee5;
      --paper: #ffffff;
      --bg: #f4f7f9;
      --accent: #176b87;
      --accent-soft: #d9eef4;
      --danger: #9b1c1c;
      --success: #116149;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Segoe UI", Arial, sans-serif;
      background: var(--bg);
      color: var(--ink);
    }}
    header {{
      border-bottom: 1px solid var(--line);
      background: var(--paper);
      padding: 24px clamp(16px, 4vw, 48px) 24px clamp(72px, 8vw, 96px);
    }}
    main {{
      width: min(860px, calc(100% - 32px));
      margin: 28px auto 48px;
      display: grid;
      gap: 18px;
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: clamp(28px, 4vw, 42px);
      letter-spacing: 0;
    }}
    h2 {{
      margin: 0 0 12px;
      font-size: 20px;
      letter-spacing: 0;
    }}
    p, li {{
      line-height: 1.55;
    }}
    .subtitle {{
      color: var(--muted);
    }}
    .back {{
      display: inline-block;
      margin-bottom: 16px;
      color: var(--accent);
      font-weight: 700;
      text-decoration: none;
    }}
    section {{
      background: var(--paper);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 20px;
    }}
    form {{
      display: grid;
      gap: 12px;
      max-width: 560px;
    }}
    label {{
      font-weight: 700;
    }}
    input[type="email"] {{
      width: 100%;
      min-height: 44px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px 12px;
      font: inherit;
    }}
    .check-row {{
      display: flex;
      align-items: center;
      gap: 9px;
      width: fit-content;
      font-weight: 600;
      cursor: pointer;
    }}
    .check-row input {{
      width: 17px;
      height: 17px;
      accent-color: var(--accent);
    }}
    button {{
      width: fit-content;
      min-height: 42px;
      border: 1px solid var(--accent);
      border-radius: 6px;
      background: var(--accent);
      color: white;
      padding: 9px 14px;
      font-weight: 700;
      cursor: pointer;
    }}
    .telegram-actions,
    .push-actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
      margin-top: 12px;
    }}
    .telegram-launch {{
      display: grid;
      gap: 6px;
    }}
    .telegram-link {{
      display: inline-flex;
      align-items: center;
      min-height: 44px;
      border: 1px solid var(--accent);
      border-radius: 6px;
      background: var(--paper);
      color: var(--accent);
      padding: 9px 14px;
      font-weight: 700;
      text-decoration: none;
    }}
    .telegram-link:hover {{ background: var(--accent-soft); }}
    .telegram-link:focus-visible {{ outline: 3px solid var(--accent-soft); outline-offset: 2px; }}
    .telegram-link.disabled {{
      border-color: var(--line);
      color: var(--muted);
      cursor: not-allowed;
    }}
    .telegram-launch-status {{
      min-height: 18px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.35;
    }}
    .hint {{
      margin: 0;
      color: var(--muted);
      font-size: 14px;
    }}
    .push-status {{
      min-height: 22px;
      margin-top: 10px;
      font-weight: 700;
    }}
    .push-status.ready {{
      color: var(--success);
    }}
    .push-status.error {{
      color: var(--danger);
    }}
    .status {{
      border-left: 5px solid var(--accent);
    }}
    .status p {{
      margin: 8px 0 0;
    }}
    .status.success {{
      border-left-color: var(--success);
    }}
    .status.error {{
      border-left-color: var(--danger);
    }}
    .suspension-summary {{
      display: grid;
      gap: 12px;
    }}
    .suspension-state {{
      width: fit-content;
      border-radius: 999px;
      padding: 5px 10px;
      font-size: 13px;
      font-weight: 800;
      background: var(--accent-soft);
      color: var(--accent);
    }}
    .suspension-state.active {{
      background: #e7f6ee;
      color: var(--success);
    }}
    .suspension-state.inactive {{
      background: #eef2f6;
      color: var(--muted);
    }}
    .suspension-dates {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      gap: 10px;
      margin: 0;
    }}
    .suspension-dates div {{
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px;
      background: #fbfdff;
    }}
    .suspension-dates dt {{
      margin: 0 0 4px;
      color: var(--muted);
      font-size: 13px;
      font-weight: 700;
    }}
    .suspension-dates dd {{
      margin: 0;
      font-weight: 700;
    }}
    .suspension-notice {{
      border-left: 4px solid var(--accent);
      background: #f2f9fc;
      border-radius: 6px;
      padding: 10px 12px;
    }}
    .suspension-editor {{
      margin-top: 14px;
    }}
    .suspension-editor summary {{
      width: fit-content;
      min-height: 42px;
      border: 1px solid var(--accent);
      border-radius: 6px;
      background: var(--paper);
      color: var(--accent);
      padding: 9px 14px;
      font-weight: 700;
      cursor: pointer;
      list-style: none;
    }}
    .suspension-editor summary::-webkit-details-marker {{
      display: none;
    }}
    .date-picker {{
      display: grid;
      gap: 12px;
      margin-top: 14px;
      max-width: 420px;
    }}
    .picker-head {{
      display: grid;
      grid-template-columns: 42px 1fr 42px;
      gap: 8px;
      align-items: center;
    }}
    .picker-head strong {{
      text-align: center;
    }}
    .picker-head button {{
      width: 42px;
      padding: 0;
    }}
    .weekday-row,
    .calendar-grid {{
      display: grid;
      grid-template-columns: repeat(7, minmax(0, 1fr));
      gap: 4px;
    }}
    .weekday-row span {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      text-align: center;
    }}
    .calendar-day {{
      width: 100%;
      min-height: 40px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--paper);
      color: var(--ink);
      padding: 0;
    }}
    .calendar-day:hover:not(:disabled) {{
      border-color: var(--accent);
      background: var(--accent-soft);
      color: var(--accent);
    }}
    .calendar-day:disabled {{
      background: #eef2f6;
      color: #9aa8b3;
      cursor: not-allowed;
    }}
    .calendar-day.range {{
      background: #dff1f6;
      border-color: #9fd0dc;
    }}
    .calendar-day.start,
    .calendar-day.end {{
      background: var(--accent);
      border-color: var(--accent);
      color: white;
    }}
    .calendar-empty {{
      min-height: 40px;
    }}
    .selection-preview {{
      min-height: 22px;
      color: var(--muted);
      font-weight: 700;
    }}
    .suspension-actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
    }}
    button.danger {{
      border-color: var(--danger);
      background: var(--danger);
    }}
    @media (max-width: 640px) {{
      main {{
        width: calc(100% - 86px);
        margin-left: 70px;
        margin-right: 16px;
      }}
    }}
    {render_sidebar_css()}
  </style>
</head>
<body class="{sidebar_classes}">
  {sidebar_markup}
  <header>
    <a class="back" href="/" aria-label="Torna alla pagina principale">&larr; Indietro</a>
    <h1>Impostazioni newsletter</h1>
    <div class="subtitle">Configura l'invio immediato delle notizie raccolte nella settimana.</div>
  </header>
    <main>
      {status}
      {account_section}
      <section>
      <h2>Iscrizione</h2>
      <form method="post" action="/newsletter">
        <label for="email">Email</label>
        <input id="email" name="email" type="email" required autocomplete="email" placeholder="nome@example.com" value="{html.escape(email_value)}">
        <p class="hint">Riceverai subito un'email con il digest settimanale disponibile. {html.escape(smtp_text)}</p>
        <button type="submit">Invia newsletter</button>
      </form>
    </section>
      <section>
        <h2>Telegram</h2>
      <p>Apri Telegram e premi <strong>Start</strong>: il digest della settimana corrente verrà inviato automaticamente in chat.</p>
      <div class="telegram-actions">
        {telegram_link}
      </div>
    </section>
    <section data-push-panel>
      <h2>Notifiche push</h2>
      <p class="hint">{html.escape(push_text)}</p>
      <div class="push-actions">
        <button type="button" id="push-enable">Attiva notifiche push</button>
      </div>
      <p class="hint push-status" id="push-status" aria-live="polite"></p>
    </section>
    {suspension_section}
      {logout_section}
    </main>
  {push_script}
  {suspension_script}
  {_render_telegram_open_script()}
</body>
</html>"""


def _render_push_notification_script() -> str:
    return f"""<script type="module">
(() => {{
  const button = document.getElementById("push-enable");
  const status = document.getElementById("push-status");
  if (!button || !status) return;

  function setStatus(message, state) {{
    status.textContent = message;
    status.classList.toggle("ready", state === "ready");
    status.classList.toggle("error", state === "error");
  }}

  async function loadFirebaseConfig() {{
    const response = await fetch("/api/firebase/config", {{
      headers: {{ "Accept": "application/json" }}
    }});
    if (!response.ok) throw new Error("Configurazione Firebase non disponibile.");
    return response.json();
  }}

  async function registerPush() {{
    button.disabled = true;
    setStatus("Richiesta autorizzazione notifiche...", "");
    try {{
      if (!("Notification" in window) || !("serviceWorker" in navigator)) {{
        throw new Error("Questo browser non supporta le notifiche push.");
      }}
      if (!window.isSecureContext) {{
        throw new Error("Le notifiche push richiedono HTTPS in produzione.");
      }}
      const config = await loadFirebaseConfig();
      if (!config.configured) {{
        throw new Error("Completa la configurazione Firebase e imposta FIREBASE_ENABLED=true.");
      }}
      const permission = await Notification.requestPermission();
      if (permission !== "granted") {{
        throw new Error("Permesso notifiche non concesso.");
      }}

      const appModule = await import("https://www.gstatic.com/firebasejs/{FIREBASE_JS_VERSION}/firebase-app.js");
      const messagingModule = await import("https://www.gstatic.com/firebasejs/{FIREBASE_JS_VERSION}/firebase-messaging.js");
      const supported = await messagingModule.isSupported();
      if (!supported) {{
        throw new Error("Firebase Messaging non \u00e8 supportato da questo browser.");
      }}

      const registration = await navigator.serviceWorker.register("/firebase-messaging-sw.js");
      const app = appModule.initializeApp(config.web);
      const messaging = messagingModule.getMessaging(app);
      const token = await messagingModule.getToken(messaging, {{
        vapidKey: config.vapidKey,
        serviceWorkerRegistration: registration
      }});
      if (!token) {{
        throw new Error("Firebase non ha restituito un token push.");
      }}

      const saveResponse = await fetch("/push/subscribe", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json", "Accept": "application/json" }},
        body: JSON.stringify({{ token }})
      }});
      const saved = await saveResponse.json();
      if (!saveResponse.ok) {{
        throw new Error(saved.message || "Registrazione token non riuscita.");
      }}
      messagingModule.onMessage(messaging, (payload) => {{
        const notification = payload.notification || {{}};
        if (Notification.permission === "granted") {{
          new Notification(notification.title || "Research Digest AI", {{
            body: notification.body || "Nuovo digest disponibile."
          }});
        }}
      }});
      setStatus(`Notifiche push attive. Dispositivi registrati: ${{saved.token_count}}.`, "ready");
    }} catch (error) {{
      setStatus(error.message || "Attivazione push non riuscita.", "error");
      button.disabled = false;
    }}
  }}

  loadFirebaseConfig()
    .then((config) => {{
      if (!config.configured) {{
        button.disabled = true;
        setStatus("Configurazione Firebase non attiva: pronta per il deploy.", "");
      }} else if (Notification.permission === "granted") {{
        setStatus("Permesso notifiche gia concesso su questo browser.", "ready");
      }} else {{
        setStatus("Premi il pulsante per ricevere notifiche push su questo browser.", "");
      }}
    }})
    .catch((error) => {{
      button.disabled = true;
      setStatus(error.message || "Configurazione Firebase non disponibile.", "error");
    }});

  button.addEventListener("click", registerPush);
}})();
</script>"""


def _render_notification_suspension_section() -> str:
    status = notifications.notification_suspension_status()
    start = status.get("start")
    end = status.get("end")
    active = bool(status.get("active"))
    configured = bool(status.get("configured"))
    state_label = "Attiva" if active else "Non attiva"
    state_class = "active" if active else "inactive"
    start_display = _format_datetime_display(start) if start else "Non impostata"
    end_display = _format_datetime_display(end) if end else "Non impostata"
    start_value = start.date().isoformat() if start else ""
    end_value = end.date().isoformat() if end else ""
    remaining = status.get("remaining") or "Non disponibile"
    today = _project_today().isoformat()
    notice = ""
    if active and start and end:
        notice = (
            '<p class="suspension-notice">Le notifiche sono attualmente sospese '
            f"dal {html.escape(start.strftime('%d/%m/%Y'))} al {html.escape(end.strftime('%d/%m/%Y'))}.</p>"
        )
    elif configured and start and end:
        notice = (
            '<p class="suspension-notice">La sospensione e programmata '
            f"dal {html.escape(start.strftime('%d/%m/%Y'))} al {html.escape(end.strftime('%d/%m/%Y'))}.</p>"
        )

    return f"""<section class="suspension-panel">
      <h2>Sospensione notifiche</h2>
      <div class="suspension-summary">
        <span class="suspension-state {state_class}">{state_label}</span>
        {notice}
        <dl class="suspension-dates">
          <div>
            <dt>Inizio</dt>
            <dd>{html.escape(start_display)}</dd>
          </div>
          <div>
            <dt>Fine</dt>
            <dd>{html.escape(end_display)}</dd>
          </div>
          <div>
            <dt>Riattivazione</dt>
            <dd>{html.escape(remaining if active else "Automatica alla fine del periodo")}</dd>
          </div>
        </dl>
      </div>
      <details class="suspension-editor">
        <summary>Modifica</summary>
        <form method="post" action="/notifications/suspension">
          <input type="hidden" id="suspension-start" name="start_date" value="{html.escape(start_value)}">
          <input type="hidden" id="suspension-end" name="end_date" value="{html.escape(end_value)}">
          <div class="date-picker" data-date-picker data-today="{html.escape(today)}">
            <div class="picker-head">
              <button type="button" id="calendar-prev" aria-label="Mese precedente">&lt;</button>
              <strong id="calendar-label"></strong>
              <button type="button" id="calendar-next" aria-label="Mese successivo">&gt;</button>
            </div>
            <div class="weekday-row" aria-hidden="true">
              <span>Lun</span><span>Mar</span><span>Mer</span><span>Gio</span><span>Ven</span><span>Sab</span><span>Dom</span>
            </div>
            <div class="calendar-grid" id="calendar-grid"></div>
            <p class="selection-preview" id="selection-preview"></p>
          </div>
          <button type="submit">Conferma periodo</button>
        </form>
      </details>
      <form method="post" action="/notifications/suspension" class="suspension-actions">
        <button type="submit" name="action" value="clear" class="danger">Annulla sospensione</button>
      </form>
    </section>"""


def _render_notification_suspension_script() -> str:
    return """<script>
(() => {
  const picker = document.querySelector("[data-date-picker]");
  if (!picker) return;

  const startInput = document.getElementById("suspension-start");
  const endInput = document.getElementById("suspension-end");
  const grid = document.getElementById("calendar-grid");
  const label = document.getElementById("calendar-label");
  const previous = document.getElementById("calendar-prev");
  const next = document.getElementById("calendar-next");
  const preview = document.getElementById("selection-preview");
  const today = parseDate(picker.dataset.today);
  let selectedStart = startInput.value || "";
  let selectedEnd = endInput.value || "";
  let currentMonth = parseDate(selectedStart || picker.dataset.today);
  currentMonth = new Date(currentMonth.getFullYear(), currentMonth.getMonth(), 1);

  function parseDate(value) {
    const parts = String(value || "").split("-").map(Number);
    return new Date(parts[0], parts[1] - 1, parts[2]);
  }

  function iso(date) {
    const month = String(date.getMonth() + 1).padStart(2, "0");
    const day = String(date.getDate()).padStart(2, "0");
    return `${date.getFullYear()}-${month}-${day}`;
  }

  function formatDate(value) {
    if (!value) return "";
    return parseDate(value).toLocaleDateString("it-IT", { day: "2-digit", month: "2-digit", year: "numeric" });
  }

  function sameDay(left, right) {
    return left && right && iso(left) === iso(right);
  }

  function isInRange(day) {
    if (!selectedStart || !selectedEnd) return false;
    const start = parseDate(selectedStart);
    const end = parseDate(selectedEnd);
    return day >= start && day <= end;
  }

  function updatePreview() {
    startInput.value = selectedStart;
    endInput.value = selectedEnd;
    if (selectedStart && selectedEnd) {
      preview.textContent = `Periodo selezionato: dal ${formatDate(selectedStart)} al ${formatDate(selectedEnd)}`;
    } else if (selectedStart) {
      preview.textContent = `Inizio selezionato: ${formatDate(selectedStart)}. Seleziona la data di fine.`;
    } else {
      preview.textContent = "Seleziona una data di inizio e una data di fine.";
    }
  }

  function choose(value) {
    if (!selectedStart || selectedEnd) {
      selectedStart = value;
      selectedEnd = "";
    } else if (parseDate(value) < parseDate(selectedStart)) {
      selectedStart = value;
      selectedEnd = "";
    } else {
      selectedEnd = value;
    }
    render();
  }

  function render() {
    grid.textContent = "";
    label.textContent = currentMonth.toLocaleDateString("it-IT", { month: "long", year: "numeric" });
    const first = new Date(currentMonth.getFullYear(), currentMonth.getMonth(), 1);
    const emptyBefore = (first.getDay() + 6) % 7;
    const daysInMonth = new Date(currentMonth.getFullYear(), currentMonth.getMonth() + 1, 0).getDate();

    for (let index = 0; index < emptyBefore; index += 1) {
      const spacer = document.createElement("span");
      spacer.className = "calendar-empty";
      grid.appendChild(spacer);
    }

    for (let dayNumber = 1; dayNumber <= daysInMonth; dayNumber += 1) {
      const day = new Date(currentMonth.getFullYear(), currentMonth.getMonth(), dayNumber);
      const value = iso(day);
      const button = document.createElement("button");
      button.type = "button";
      button.className = "calendar-day";
      button.textContent = String(dayNumber);
      button.disabled = day < today;
      if (isInRange(day)) button.classList.add("range");
      if (sameDay(day, parseDate(selectedStart))) button.classList.add("start");
      if (sameDay(day, parseDate(selectedEnd))) button.classList.add("end");
      button.addEventListener("click", () => choose(value));
      grid.appendChild(button);
    }
    updatePreview();
  }

  previous.addEventListener("click", () => {
    currentMonth = new Date(currentMonth.getFullYear(), currentMonth.getMonth() - 1, 1);
    render();
  });
  next.addEventListener("click", () => {
    currentMonth = new Date(currentMonth.getFullYear(), currentMonth.getMonth() + 1, 1);
    render();
  });
  render();
})();
</script>"""


def _format_datetime_display(value: datetime) -> str:
    local_value = value.astimezone() if value.tzinfo else value
    return local_value.strftime("%d/%m/%Y %H:%M")


def load_gui_env() -> None:
    load_dotenv(dotenv_path=Path.cwd() / ".env", override=True)


def configure_gui_logging() -> None:
    """Configure observable server-side logs without exposing Telegram secrets."""

    level_name = os.getenv("TELEGRAM_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )
    logger.setLevel(level)
    # httpx includes request URLs in its INFO messages. Telegram bot tokens are
    # embedded in Bot API URLs, so transport logs must stay below INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def web_runtime_config() -> dict:
    """Return public, runtime-configurable settings for the browser client."""

    try:
        config = load_config(os.getenv("RESEARCH_DIGEST_CONFIG_PATH", "config.yaml"))
    except (FileNotFoundError, ValueError):
        config = {}
    web_config = config.get("web", {}) if isinstance(config, dict) else {}
    if not isinstance(web_config, dict):
        web_config = {}
    configured_base = os.getenv("RESEARCH_DIGEST_API_BASE_URL") or web_config.get("api_base_url") or "/api"
    api_base_url = _safe_api_base_path(configured_base)
    configured_timeout = os.getenv("RESEARCH_DIGEST_REQUEST_TIMEOUT_MS") or web_config.get(
        "request_timeout_ms", 20_000
    )
    try:
        request_timeout_ms = max(1_000, min(int(configured_timeout), 120_000))
    except (TypeError, ValueError):
        request_timeout_ms = 20_000
    schedule = config.get("schedule", {}) if isinstance(config, dict) else {}
    if not isinstance(schedule, dict):
        schedule = {}
    timezone_name = str(web_config.get("timezone") or schedule.get("timezone") or "UTC").strip() or "UTC"
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        timezone_name = "UTC"
    return {
        "api_base_url": api_base_url,
        "request_timeout_ms": request_timeout_ms,
        "environment": str(os.getenv("RESEARCH_DIGEST_ENV") or web_config.get("environment") or "production"),
        "timezone": timezone_name,
        "features": {
            # The newsletter service is available even without SMTP because the
            # existing outbox is an intentional delivery fallback.
            "newsletter": True,
            "telegram": _telegram_onboarding_ready(),
            "push": bool(public_firebase_config().get("configured")),
        },
    }


def _safe_api_base_path(value: object) -> str:
    """Only expose a same-origin, path-based API base to the browser client."""

    candidate = str(value or "").strip()
    if not candidate:
        return "/api"
    parsed = urlparse(candidate)
    decoded_segments = unquote(candidate).split("/")
    if (
        not candidate.startswith("/")
        or candidate.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or "\x00" in candidate
        or any(segment in {".", ".."} for segment in decoded_segments)
    ):
        return "/api"
    normalized = candidate.rstrip("/")
    return normalized or "/api"


def _project_now(timezone_name: str | None = None) -> datetime:
    """Return a naive wall-clock value in the configured project time zone.

    Account suspension records intentionally contain date-only ranges. The
    notifications module stores their boundary values without a time-zone
    object, so callers compare them against this project-zone wall clock rather
    than the host machine's local time.
    """

    name = timezone_name or web_runtime_config()["timezone"]
    try:
        timezone = ZoneInfo(name)
    except ZoneInfoNotFoundError:
        timezone = ZoneInfo("UTC")
    return datetime.now(timezone).replace(tzinfo=None)


def _project_today() -> date:
    return _project_now().date()


def archived_digest_history() -> list[DigestHistoryItem]:
    """Return every published digest except the homepage's latest snapshot."""

    history = weekly_digest_history()
    return history[1:] if history else []


def _canonical_api_request_path(request_path: str) -> str:
    """Map the configured public API prefix back to the handler's routes.

    ``/api/config`` is deliberately kept as the fixed bootstrap endpoint. Once
    the browser has loaded runtime configuration it may use a deployment
    prefix such as ``/api/v2``; both forms resolve to the same server contract.
    """

    requested = str(request_path or "")
    configured_base = web_runtime_config()["api_base_url"]
    if configured_base == "/api":
        return requested
    if requested == configured_base:
        return "/api"
    if requested.startswith(f"{configured_base}/"):
        return f"/api{requested[len(configured_base):]}"
    return requested


def _static_asset_path(request_path: str) -> Path | None:
    """Map a public /assets path to the imported UI bundle without traversal."""

    normalized_path = unquote(str(request_path or ""))
    if not normalized_path.startswith("/assets/"):
        return None
    relative_path = normalized_path.removeprefix("/assets/")
    if not relative_path or "\x00" in relative_path:
        return None
    asset_root = (WEB_ASSET_ROOT / "assets").resolve()
    try:
        candidate = (asset_root / relative_path).resolve()
        candidate.relative_to(asset_root)
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_file() else None


def _static_content_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(str(path))
    if path.suffix.casefold() == ".js":
        return "application/javascript; charset=utf-8"
    if path.suffix.casefold() == ".css":
        return "text/css; charset=utf-8"
    if path.suffix.casefold() in {".html", ".htm"}:
        return "text/html; charset=utf-8"
    return guessed or "application/octet-stream"


def _safe_public_url(value: object) -> str:
    """Keep external links HTTP(S)-only before they reach the browser UI."""

    candidate = str(value or "").strip()
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return ""
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return ""
    return candidate


def _api_news_record(record: dict, saved_ids: set[str] | None = None) -> dict:
    meta = _telegram_entry_meta(str(record.get("meta") or ""))
    news_id = str(record.get("id") or news_entry_id(record))
    category = _display_category(meta.get("category") or "") or "Non classificata"
    return {
        "id": news_id,
        "title": normalize_news_title(record.get("title") or "Senza titolo"),
        "category": category,
        "source": str(meta.get("source") or "Fonte non disponibile"),
        "url": _safe_public_url(record.get("url")),
        "summary": str(record.get("summary") or ""),
        "why": str(record.get("why") or ""),
        "publication_date": str(meta.get("date") or ""),
        "relevance": str(meta.get("relevance") or ""),
        "digest_date": str(record.get("digest_date") or ""),
        "digest_title": str(record.get("digest_title") or ""),
        "saved": news_id in (saved_ids or set()),
    }


def _validated_assistant_context(raw_context: object) -> dict:
    raw_context = raw_context or {}
    if not isinstance(raw_context, dict):
        raise ValueError("Il contesto della notizia non è valido.")
    title = str(raw_context.get("title") or "").strip()[:500]
    content = str(raw_context.get("content") or "").strip()
    source = str(raw_context.get("source") or "").strip()[:300]
    raw_url = str(raw_context.get("url") or "").strip()
    source_url = _safe_public_url(raw_url)
    if len(content) > ASSISTANT_CONTEXT_LIMIT:
        content = content[:ASSISTANT_CONTEXT_LIMIT]
    if raw_url and not source_url:
        raise ValueError("Il link della fonte non è valido.")
    return {
        "title": title,
        "content": content,
        "source": source,
        "url": source_url,
    }


def _validated_assistant_attachment(raw_attachment: object) -> dict | None:
    if raw_attachment is None:
        return None
    if not isinstance(raw_attachment, dict):
        raise ValueError("L'allegato non è valido.")

    filename = str(raw_attachment.get("filename") or "allegato").strip()[:255]
    mime_type = str(raw_attachment.get("mimeType") or raw_attachment.get("mime_type") or "").strip().lower()
    data_value = str(raw_attachment.get("data") or "").strip()
    if not data_value:
        raise ValueError("Il file allegato è vuoto o non è stato caricato correttamente.")

    extension = Path(filename).suffix.lower()
    if mime_type not in ASSISTANT_ATTACHMENT_ALLOWED_MIME_TYPES and extension not in ASSISTANT_ATTACHMENT_ALLOWED_EXTENSIONS:
        raise ValueError("Formato file non supportato. Sono ammessi JPEG, PNG, PDF e TXT.")

    # Normalize the mime type when only the extension was reliable (e.g. browser omitted it).
    if mime_type not in ASSISTANT_ATTACHMENT_ALLOWED_MIME_TYPES:
        mime_type = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".pdf": "application/pdf",
            ".txt": "text/plain",
        }.get(extension, "")
    if not mime_type:
        raise ValueError("Formato file non supportato. Sono ammessi JPEG, PNG, PDF e TXT.")

    try:
        binary_data = base64.b64decode(data_value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("Il file allegato non è leggibile.") from exc

    if not binary_data:
        raise ValueError("Il file allegato è vuoto o non è stato caricato correttamente.")
    if len(binary_data) > ASSISTANT_ATTACHMENT_MAX_BYTES:
        raise ValueError("Il file supera la dimensione massima consentita di 8 MB.")

    return {
        "filename": filename or "allegato",
        "mime_type": mime_type,
        "data": binary_data,
    }


def _validated_assistant_audio(raw_audio: object) -> dict:
    """Validates a recorded voice message before it is sent for transcription.

    Mirrors _validated_assistant_attachment's checks (format allow-list, decodability,
    size cap) but against the separate, audio-specific limits above.
    """
    if not isinstance(raw_audio, dict):
        raise ValueError("Il messaggio vocale non è valido.")

    filename = str(raw_audio.get("filename") or "messaggio_vocale").strip()[:255]
    mime_type = str(raw_audio.get("mimeType") or raw_audio.get("mime_type") or "").strip().lower()
    data_value = str(raw_audio.get("data") or "").strip()
    if not data_value:
        raise ValueError("Il messaggio vocale è vuoto o non è stato caricato correttamente.")

    extension = Path(filename).suffix.lower()
    if mime_type not in ASSISTANT_AUDIO_ALLOWED_MIME_TYPES and extension not in ASSISTANT_AUDIO_ALLOWED_EXTENSIONS:
        raise ValueError("Formato audio non supportato. Registra il messaggio vocale dall'app e riprova.")

    if mime_type not in ASSISTANT_AUDIO_ALLOWED_MIME_TYPES:
        mime_type = ASSISTANT_AUDIO_EXTENSION_TO_MIME.get(extension, "")
    if not mime_type:
        raise ValueError("Formato audio non supportato. Registra il messaggio vocale dall'app e riprova.")

    try:
        binary_data = base64.b64decode(data_value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("Il messaggio vocale non è leggibile.") from exc

    if not binary_data:
        raise ValueError("Il messaggio vocale è vuoto o non è stato caricato correttamente.")
    if len(binary_data) > ASSISTANT_AUDIO_MAX_BYTES:
        raise ValueError("Il messaggio vocale supera la dimensione massima consentita.")

    return {
        "filename": filename or "messaggio_vocale",
        "mime_type": mime_type,
        "data": binary_data,
    }


def _assistant_request_payload(payload: dict) -> tuple[str, dict, list[dict], dict | None, str]:
    message = str(payload.get("message") or "").strip()
    attachment = _validated_assistant_attachment(payload.get("attachment"))
    if not message and not attachment:
        raise ValueError("Scrivi un messaggio o allega un file prima di inviare.")
    if len(message) > ASSISTANT_MESSAGE_LIMIT:
        raise ValueError("Il messaggio è troppo lungo.")

    response_format = str(payload.get("response_format") or "text").strip().casefold()
    if response_format not in {"text", "audio"}:
        response_format = "text"

    context = _validated_assistant_context(payload.get("context"))
    if ASSISTANT_COMPARISON_PATTERN.search(message):
        comparison = _validated_assistant_context(payload.get("comparison_context"))
        if comparison.get("title") or comparison.get("content"):
            context["comparison"] = comparison

    raw_history = payload.get("history") or []
    if not isinstance(raw_history, list):
        raise ValueError("La cronologia della chat non è valida.")
    history: list[dict] = []
    for item in raw_history[-ASSISTANT_HISTORY_LIMIT:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().casefold()
        content_value = str(item.get("content") or "").strip()[:ASSISTANT_MESSAGE_LIMIT]
        if role in {"user", "assistant"} and content_value:
            history.append({"role": role, "content": content_value})
    return message, context, history, attachment, response_format


def _api_digest_payload(view: DigestView, user: dict | None = None) -> dict:
    saved_ids = saved_news_ids_for_user(user) if user else set()
    items = []
    for entry in view.entries:
        items.append(_api_news_record(_news_record(entry, view), saved_ids))
    return {
        "title": str(view.title or "Research Digest"),
        "subtitle": str(view.subtitle or ""),
        "digest_date": str(view.digest_date or ""),
        **_iso_week_bounds_payload(str(view.digest_date or "")),
        "warning": str(view.warning or ""),
        "items": items,
    }


def _api_digest_for_request(query: dict[str, list[str]], user: dict | None = None) -> dict | None:
    digest_id = str((query.get("id") or [""])[0]).strip()
    digest_date = str((query.get("date") or [""])[0]).strip()
    if digest_date:
        if _parse_iso_date(digest_date) is None:
            return None
        view = load_digest_week_by_date(digest_date)
    elif digest_id:
        view = load_digest_by_id(digest_id)
    else:
        view = load_current_digest()
    return _api_digest_payload(view, user) if view else None


def _history_query_matches(item: DigestHistoryItem, view: DigestView, query: str) -> bool:
    normalized_query = " ".join(query.casefold().split())
    if not normalized_query:
        return True
    values = [item.title, item.subtitle, item.digest_date or "", item.updated, view.title, view.subtitle]
    for entry in view.entries:
        values.extend(
            [
                str(entry.get("title") or ""),
                str(entry.get("meta") or ""),
                str(entry.get("summary") or ""),
                str(entry.get("why") or ""),
            ]
        )
    return normalized_query in " ".join(values).casefold()


def _api_history_page(
    user: dict,
    *,
    query: str = "",
    offset: int = 0,
    limit: int = 12,
) -> dict:
    """Return only a page of metadata; full digest cards load on selection."""

    saved_ids = saved_news_ids_for_user(user)
    matches: list[dict] = []
    for item in archived_digest_history():
        view = load_digest_week_by_date(item.digest_date) if item.digest_date else load_digest_by_id(item.id)
        if view is None or not _history_query_matches(item, view, query):
            continue
        # Ogni sezione dell'accordion rappresenta un'intera settimana ISO
        # (vedi weekly_digest_history/archived_digest_history): qui vanno
        # quindi inclusi TUTTI i titoli delle notizie di quella settimana,
        # non solo i primi, per offrire una panoramica completa. Il
        # contenuto completo (sintesi, perché conta, link...) resta
        # comunque visibile solo aprendo il digest, mai in questa anteprima.
        previews = [_api_news_record(_news_record(entry, view), saved_ids) for entry in view.entries]
        matches.append(
            {
                "id": item.id,
                "digest_date": str(item.digest_date or ""),
                **_iso_week_bounds_payload(str(item.digest_date or "")),
                "title": str(item.title or "Research Digest"),
                "subtitle": str(item.subtitle or ""),
                "updated": str(item.updated or ""),
                "entry_count": int(item.entry_count or len(view.entries)),
                "previews": previews,
            }
        )
    bounded_offset = max(0, offset)
    bounded_limit = max(1, min(limit, 50))
    page = matches[bounded_offset : bounded_offset + bounded_limit]
    next_offset = bounded_offset + len(page)
    return {
        "items": page,
        "query": query,
        "offset": bounded_offset,
        "limit": bounded_limit,
        "total": len(matches),
        "next_offset": next_offset if next_offset < len(matches) else None,
    }


def _api_suspension_payload(user: dict) -> dict:
    runtime = web_runtime_config()
    status = notifications.user_notification_suspension_status(
        str(user.get("id") or ""),
        now=_project_now(runtime["timezone"]),
    )
    start = status.get("start")
    end = status.get("end")
    return {
        "configured": bool(status.get("configured")),
        "active": bool(status.get("active")),
        "start": start.date().isoformat() if isinstance(start, datetime) else "",
        "end": end.date().isoformat() if isinstance(end, datetime) else "",
        "remaining": str(status.get("remaining") or ""),
        "timezone": runtime["timezone"],
    }


def _api_settings_payload(user: dict) -> dict:
    push = public_firebase_config()
    return {
        "profile": {
            "first_name": str(user.get("first_name") or ""),
            "last_name": str(user.get("last_name") or ""),
            "email": str(user.get("email") or ""),
        },
        "preferences": {
            "email": bool((user.get("preferences") or {}).get("email", True)),
            "telegram": bool((user.get("preferences") or {}).get("telegram", False)),
        },
        "telegram": {
            "configured": _telegram_onboarding_ready(),
            "connected": bool(user.get("telegram_connected")),
            # An onboarding intent is deliberately generated only after the
            # user presses the Telegram action, not on each settings refresh.
            "onboarding_url": "",
            "native_onboarding_url": "",
        },
        "push": {
            "enabled": bool(push.get("enabled")),
            "configured": bool(push.get("configured")),
        },
        "newsletter": {
            "available": True,
            "smtp_configured": _smtp_configured(),
        },
        "suspension": _api_suspension_payload(user),
    }


def _json_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    return default


def run_server(host: str = "127.0.0.1", port: int = 8765) -> None:
    load_gui_env()
    configure_gui_logging()
    _init_firestore_stores()
    if _FIRESTORE_ENABLED:
        try:
            import subprocess
            logger.info("Firestore attivo — eseguo git pull per aggiornare i digest .md")
            result = subprocess.run(["git", "pull"], capture_output=True, text=True, timeout=30)
            logger.info("git pull: %s", result.stdout.strip() or result.stderr.strip())
        except Exception as exc:
            logger.warning("git pull non riuscito (il server parte comunque): %s", exc)
    start_telegram_start_poller()
    server = ThreadingHTTPServer((host, port), DigestRequestHandler)
    print(f"Interfaccia digest disponibile su http://{host}:{port}")
    server.serve_forever()


class DigestRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = _canonical_api_request_path(parsed.path)
        query = parse_qs(parsed.query)
        request_path = self._request_path(parsed)
        user = self._current_user()
        if path.startswith("/assets/"):
            asset = _static_asset_path(path)
            if asset is None:
                self.send_error(404)
                return
            try:
                payload = asset.read_bytes()
            except OSError:
                self.send_error(404)
                return
            self._send(
                payload,
                _static_content_type(asset),
                # Asset names are intentionally human-readable rather than
                # fingerprinted by a bundler. Revalidate them on deployment so
                # an older cached module cannot run against a newer API.
                headers={"Cache-Control": "public, max-age=0, must-revalidate"},
            )
            return
        if path == "/api/config":
            self._send_json(web_runtime_config())
            return
        if path == "/api/me":
            if user is None:
                self._send_json({"ok": False, "message": "Sessione non attiva."}, status=401)
                return
            self._send_json({"ok": True, "user": user})
            return
        if path in {"/api/digest/current", "/api/digest", "/api/digests", "/api/saved", "/api/settings", "/api/telegram/onboarding"} and user is None:
            self._send_json({"ok": False, "message": "Accedi per consultare il digest."}, status=401)
            return
        if path == "/api/digest/current":
            payload = _api_digest_for_request({}, user)
            if payload is None:
                self._send_json({"ok": False, "message": "Digest non trovato."}, status=404)
                return
            self._send_json({"ok": True, "digest": payload})
            return
        if path == "/api/digest":
            payload = _api_digest_for_request(query, user)
            if payload is None:
                self._send_json({"ok": False, "message": "Digest non trovato."}, status=404)
                return
            self._send_json({"ok": True, "digest": payload})
            return
        if path == "/api/digests":
            raw_limit = str((query.get("limit") or ["12"])[0])
            raw_offset = str((query.get("offset") or ["0"])[0])
            try:
                limit = int(raw_limit)
                offset = int(raw_offset)
            except ValueError:
                self._send_json({"ok": False, "message": "Paginazione non valida."}, status=400)
                return
            if offset < 0 or limit < 1:
                self._send_json({"ok": False, "message": "Paginazione non valida."}, status=400)
                return
            search = str((query.get("q") or [""])[0]).strip()[:200]
            self._send_json({"ok": True, **_api_history_page(user, query=search, offset=offset, limit=limit)})
            return
        if path == "/api/saved":
            saved_records = [_api_news_record(record, {str(record.get("id") or "")}) for record in saved_news_for_user(user)]
            self._send_json({"ok": True, "items": saved_records})
            return
        if path == "/api/settings":
            self._send_json({"ok": True, "settings": _api_settings_payload(user)})
            return
        if path == "/api/telegram/onboarding":
            settings = _api_settings_payload(user)
            telegram = settings["telegram"]
            if telegram["configured"] and not telegram["connected"]:
                link_pair = _telegram_account_link_pair(user)
                if link_pair:
                    telegram["native_onboarding_url"], telegram["onboarding_url"] = link_pair
            self._send_json({"ok": True, "telegram": settings["telegram"]})
            return
        if path in {"/", "/index.html", "/digest", "/today", "/login", "/register", "/settings", "/saved", "/history", "/assistant"}:
            try:
                payload = WEB_INDEX_PATH.read_bytes()
            except OSError:
                self.send_error(503, "Frontend non disponibile")
                return
            self._send(payload, "text/html; charset=utf-8", headers={"Cache-Control": "no-store"})
            return
        if path in {"/login", "/register"}:
            next_path = _safe_next_path((query.get("next") or ["/"])[0])
            if user:
                self._redirect(next_path)
                return
            mode = "register" if path == "/register" else "login"
            self._send(render_auth_html(mode, next_path=next_path).encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/firebase/config":
            self._send_json({"ok": True, **public_firebase_config()})
            return
        if path == "/api/telegram/status":
            if user is None:
                self._send_json({"ok": False, "message": "Accedi per verificare Telegram."}, status=401)
                return
            self._send_json(
                {
                    "ok": True,
                    "configured": _telegram_onboarding_ready(),
                    "connected": bool(user.get("telegram_connected")),
                    "notifications_enabled": bool((user.get("preferences") or {}).get("telegram")),
                }
            )
            return
        if path == "/firebase-messaging-sw.js":
            self._send(
                render_firebase_messaging_service_worker().encode("utf-8"),
                "application/javascript; charset=utf-8",
            )
            return
        if path.startswith("/share/") and not user:
            self._redirect_to_login(request_path)
            return
        if path == "/history":
            self._redirect("/?sidebar=open")
            return
        if path in {"/api/latest", "/api/history"} and user is None:
            self._send_json({"ok": False, "message": "Accedi per consultare il digest."}, status=401)
            return
        if path == "/settings":
            user = user or self._require_user(request_path)
            if user is None:
                return
            self._send(
                render_settings_html(user=user, current_path=request_path).encode("utf-8"),
                "text/html; charset=utf-8",
            )
            return
        if path == "/saved":
            self._redirect("/?sidebar=saved")
            return
        if path.startswith("/news/"):
            if user is None:
                self._redirect_to_login(request_path)
                return
        if path.startswith("/news/") or path.startswith("/share/"):
            news_id = path.rsplit("/", 1)[-1]
            record = find_news_record(news_id)
            if record is None and user is not None:
                record = saved_news_record_for_user(user, news_id)
            if record is None:
                self.send_error(404)
                return
            self._send(
                render_news_detail_html(
                    record,
                    user,
                    current_path=request_path,
                    shared=path.startswith("/share/"),
                ).encode("utf-8"),
                "text/html; charset=utf-8",
            )
            return
        digest_id = query.get("id", [None])[0]
        digest_date = query.get("date", [None])[0]
        if path in {"/", "/index.html", "/today"} and not digest_id and not digest_date:
            view = load_current_digest()
        elif digest_date:
            view = load_digest_week_by_date(digest_date)
            if view is None and digest_date == _project_today().isoformat():
                view = load_current_digest()
        else:
            view = load_digest_by_id(digest_id) if digest_id else load_latest_digest()
        if view is None:
            self.send_error(404)
            return
        if path == "/api/latest":
            payload = json.dumps(asdict(view), ensure_ascii=False).encode("utf-8")
            self._send(payload, "application/json; charset=utf-8")
            return
        if path == "/api/history":
            payload = json.dumps([asdict(item) for item in archived_digest_history()], ensure_ascii=False).encode("utf-8")
            self._send(payload, "application/json; charset=utf-8")
            return
        if path in {"/", "/index.html", "/digest", "/today"}:
            if user is None:
                self._redirect_to_login(request_path)
                return
            sidebar_section = _sidebar_requested_section(request_path)
            show_history_sidebar = (
                path in {"/", "/index.html", "/today"} and sidebar_section == "open"
            )
            show_saved_sidebar = (
                path in {"/", "/index.html", "/today"} and sidebar_section == "saved"
            )
            history_items = archived_digest_history() if show_history_sidebar else None
            history_records = (
                _history_news_records(history_items) if history_items is not None else None
            )
            self._send(
                render_html(
                    view,
                    history=history_items,
                    user=user,
                    history_search_records=history_records,
                    show_history_sidebar=show_history_sidebar,
                    saved_sidebar_content=(
                        _render_saved_sidebar_panel(saved_news_for_user(user))
                        if show_saved_sidebar
                        else ""
                    ),
                    current_path=request_path,
                    active_section="saved" if show_saved_sidebar else None,
                    back_href="/" if path == "/digest" and (digest_date or digest_id) else None,
                ).encode("utf-8"),
                "text/html; charset=utf-8",
            )
            return
        self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = _canonical_api_request_path(parsed.path)
        if not self._is_same_origin_request():
            if path.startswith("/api/"):
                self._send_json({"ok": False, "message": "Origine della richiesta non valida."}, status=403)
            else:
                self.send_error(403, "Origine della richiesta non valida.")
            return
        if path in {"/api/auth/login", "/api/auth/register"}:
            payload = self._read_json_object()
            if payload is None:
                return
            if path == "/api/auth/register":
                first_name = str(payload.get("first_name") or "")
                last_name = str(payload.get("last_name") or "")
                email_address = str(payload.get("email") or "")
                password = str(payload.get("password") or "")
                password_confirmation = str(payload.get("password_confirmation") or "")
                errors = auth.validate_registration(
                    first_name,
                    last_name,
                    email_address,
                    password,
                    password_confirmation,
                )
                if errors:
                    self._send_json(
                        {"ok": False, "message": next(iter(errors.values())), "errors": errors}, status=422
                    )
                    return
                result = auth.register_user(
                    first_name,
                    last_name,
                    email_address,
                    password,
                    password_confirmation,
                    email_notifications=_json_bool(payload.get("email_notifications"), True),
                    telegram_notifications=False,
                )
                if not result.ok or result.user is None:
                    self._send_json({"ok": False, "message": result.message}, status=409)
                    return
                if result.user["preferences"]["email"]:
                    _save_subscriber(result.user["email"])
                persistent = _json_bool(payload.get("persistent"), False)
                token, expires_at = auth.create_session(str(result.user["id"]), persistent=persistent)
                self._send_json(
                    {"ok": True, "message": result.message, "user": result.user},
                    status=201,
                    headers={"Set-Cookie": self._session_cookie_header(token, expires_at, persistent=persistent)},
                )
                return

            result = auth.authenticate_user(str(payload.get("email") or ""), str(payload.get("password") or ""))
            if not result.ok or result.user is None:
                self._send_json({"ok": False, "message": result.message}, status=401)
                return
            persistent = _json_bool(payload.get("persistent"), False)
            token, expires_at = auth.create_session(str(result.user["id"]), persistent=persistent)
            self._send_json(
                {"ok": True, "message": result.message, "user": result.user},
                headers={"Set-Cookie": self._session_cookie_header(token, expires_at, persistent=persistent)},
            )
            return
        if path == "/api/auth/logout":
            auth.revoke_session(self._session_token())
            self._send_json(
                {"ok": True, "message": "Sessione terminata."},
                headers={"Set-Cookie": self._expired_session_cookie_header()},
            )
            return
        if path in {
            "/api/assistant",
            "/api/assistant/speech",
            "/api/assistant/transcribe",
            "/api/saved",
            "/api/settings/profile",
            "/api/settings/preferences",
            "/api/settings/suspension",
            "/api/newsletter",
            "/api/push/status",
            "/api/push/subscribe",
            "/api/push/unsubscribe",
        }:
            user = self._current_user()
            if user is None:
                self._send_json({"ok": False, "message": "Sessione scaduta. Accedi di nuovo."}, status=401)
                return
            if path == "/api/saved":
                self._handle_api_saved_news(user)
                return
            payload = self._read_json_object(
                maximum_bytes=(
                    ASSISTANT_ATTACHMENT_PAYLOAD_LIMIT if path == "/api/assistant"
                    else ASSISTANT_AUDIO_PAYLOAD_LIMIT if path == "/api/assistant/transcribe"
                    else 32_768
                )
            )
            if payload is None:
                return
            if path == "/api/assistant":
                try:
                    message, context, history, attachment, response_format = _assistant_request_payload(payload)
                except ValueError as exc:
                    self._send_json({"ok": False, "message": str(exc)}, status=422)
                    return
                try:
                    answer = answer_news_question(
                        message, context, history, attachment=attachment, response_format=response_format
                    )
                except AssistantAttachmentError as exc:
                    logger.warning("Allegato Nuvia Assistant non elaborabile: %s", exc)
                    self._send_json({"ok": False, "message": str(exc)}, status=422)
                    return
                except Exception:
                    logger.exception("Risposta Nuvia Assistant non riuscita")
                    self._send_json(
                        {
                            "ok": False,
                            "message": "Nuvia non riesce a rispondere in questo momento. Riprova tra poco.",
                        },
                        status=502,
                    )
                    return
                needs_clarification = answer.startswith(ASSISTANT_CLARIFICATION_PREFIX)
                if needs_clarification:
                    answer = answer[len(ASSISTANT_CLARIFICATION_PREFIX):].strip()
                    if not answer:
                        answer = "Puoi fornire qualche dettaglio in più sulla tua richiesta?"
                # The off-topic fallback is intentionally exposed as a dedicated flag so
                # every client renders it immediately as plain text, without offering or
                # generating an audio version. Normalize harmless punctuation/whitespace
                # variations from the model back to the one exact product copy.
                normalized_answer = answer.strip().rstrip(" .!?").casefold()
                normalized_fallback = ASSISTANT_OFF_TOPIC_ANSWER.rstrip(" .!?").casefold()
                off_topic = normalized_answer == normalized_fallback
                if off_topic:
                    answer = ASSISTANT_OFF_TOPIC_ANSWER
                    needs_clarification = False
                self._send_json({
                    "ok": True,
                    "answer": answer,
                    "needs_clarification": needs_clarification,
                    "off_topic": off_topic,
                })
                return
            if path == "/api/assistant/speech":
                text = str(payload.get("text") or "").strip()
                if not text:
                    self._send_json(
                        {"ok": False, "message": "Nessun testo disponibile da convertire in audio."}, status=422
                    )
                    return
                try:
                    audio_bytes = synthesize_speech(text)
                except AssistantSpeechError as exc:
                    logger.warning("Sintesi vocale Nuvia Assistant non riuscita: %s", exc)
                    self._send_json({"ok": False, "message": str(exc)}, status=502)
                    return
                except Exception:
                    logger.exception("Errore inatteso nella sintesi vocale Nuvia Assistant")
                    self._send_json(
                        {"ok": False, "message": "Impossibile generare l'audio in questo momento. Riprova."},
                        status=502,
                    )
                    return
                self._send_json(
                    {
                        "ok": True,
                        "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
                        "mime": "audio/wav",
                    }
                )
                return
            if path == "/api/assistant/transcribe":
                try:
                    audio = _validated_assistant_audio(payload.get("audio"))
                except ValueError as exc:
                    self._send_json({"ok": False, "message": str(exc)}, status=422)
                    return
                try:
                    transcript = transcribe_audio(audio["data"], audio["mime_type"])
                except AssistantTranscriptionError as exc:
                    logger.warning("Trascrizione messaggio vocale Nuvia Assistant non riuscita: %s", exc)
                    self._send_json({"ok": False, "message": str(exc)}, status=422)
                    return
                except Exception:
                    logger.exception("Errore inatteso nella trascrizione del messaggio vocale Nuvia Assistant")
                    self._send_json(
                        {"ok": False, "message": "Impossibile trascrivere il messaggio vocale ora. Riprova."},
                        status=502,
                    )
                    return
                # The transcript is returned only so the client can immediately forward it,
                # unmodified, as the "message" of the next /api/assistant call (subject to
                # the exact same rules/memory/history as typed text); the client never
                # renders this transcript to the user - only the recorded audio bubble and
                # the assistant's final text/audio answer are shown in the chat UI.
                self._send_json({"ok": True, "transcript": transcript})
                return
            if path == "/api/settings/profile":
                previous_email = str(user.get("email") or "")
                result = auth.update_user_email(str(user.get("id") or ""), str(payload.get("email") or ""))
                if not result.ok or result.user is None:
                    self._send_json({"ok": False, "message": result.message}, status=422)
                    return
                if result.user["preferences"]["email"]:
                    _replace_subscriber(previous_email, result.user["email"])
                else:
                    _remove_subscriber(previous_email)
                self._send_json({"ok": True, "message": result.message, "settings": _api_settings_payload(result.user)})
                return
            if path == "/api/settings/preferences":
                email_enabled = _json_bool(payload.get("email"), bool((user.get("preferences") or {}).get("email", True)))
                requested_telegram = _json_bool(
                    payload.get("telegram"), bool((user.get("preferences") or {}).get("telegram", False))
                )
                telegram_enabled = requested_telegram and bool(user.get("telegram_connected"))
                result = auth.update_notification_preferences(
                    str(user.get("id") or ""),
                    email_notifications=email_enabled,
                    telegram_notifications=telegram_enabled,
                )
                if not result.ok or result.user is None:
                    self._send_json({"ok": False, "message": result.message}, status=422)
                    return
                if result.user["preferences"]["email"]:
                    _save_subscriber(result.user["email"])
                else:
                    _remove_subscriber(result.user["email"])
                _sync_account_telegram_subscription(result.user)
                message = result.message
                if requested_telegram and not user.get("telegram_connected"):
                    message = "Collega prima Telegram per attivare questo canale."
                self._send_json({"ok": True, "message": message, "settings": _api_settings_payload(result.user)})
                return
            if path == "/api/settings/suspension":
                action = str(payload.get("action") or "save").strip().casefold()
                if action == "clear":
                    notifications.clear_user_notification_suspension(str(user.get("id") or ""))
                    self._send_json(
                        {
                            "ok": True,
                            "message": "L'invio delle notifiche è stato riattivato.",
                            "suspension": _api_suspension_payload(user),
                        }
                    )
                    return
                start = _parse_iso_date(str(payload.get("start_date") or ""))
                end = _parse_iso_date(str(payload.get("end_date") or ""))
                if start is None or end is None or end < start or start < _project_today():
                    self._send_json(
                        {
                            "ok": False,
                            "message": "Seleziona un intervallo valido con date non passate e fine non precedente all'inizio.",
                        },
                        status=422,
                    )
                    return
                notifications.save_user_notification_suspension(str(user.get("id") or ""), start, end)
                self._send_json(
                    {
                        "ok": True,
                        "message": "Sospensione notifiche aggiornata.",
                        "suspension": _api_suspension_payload(user),
                    }
                )
                return
            if path == "/api/newsletter":
                requested_email = str(payload.get("email") or user.get("email") or "").strip()
                if requested_email.casefold() != str(user.get("email") or "").casefold():
                    self._send_json(
                        {"ok": False, "message": "Aggiorna prima l'email del profilo per usare un nuovo indirizzo."},
                        status=422,
                    )
                    return
                result = subscribe_and_send_newsletter(requested_email, user_id=str(user.get("id") or ""))
                self._send_json(
                    {
                        "ok": result.ok,
                        "title": result.title,
                        "message": result.message,
                        "delivered": result.delivered,
                    },
                    status=200 if result.ok else 502,
                )
                return
            if path == "/api/push/status":
                self._handle_api_push_status(user, payload)
                return
            if path == "/api/push/subscribe":
                self._handle_api_push_subscription(user, payload)
                return
            if path == "/api/push/unsubscribe":
                self._handle_api_push_unsubscribe(user, payload)
                return
        if path == "/api/saved":
            user = self._current_user()
            if user is None:
                self._send(
                    json.dumps(
                        {"ok": False, "message": "Accedi per gestire le notizie salvate."},
                        ensure_ascii=False,
                    ).encode("utf-8"),
                    "application/json; charset=utf-8",
                    status=401,
                )
                return
            self._handle_saved_news(user)
            return
        if path in {"/login", "/register"}:
            fields = self._read_form_fields()
            next_path = _safe_next_path(_form_value(fields, "next"))
            if path == "/register":
                values = {
                    "first_name": _form_value(fields, "first_name"),
                    "last_name": _form_value(fields, "last_name"),
                    "email": _form_value(fields, "email"),
                    "email_notifications": "1" if _form_checked(fields, "email_notifications") else "0",
                    "telegram_notifications": "1" if _form_checked(fields, "telegram_notifications") else "0",
                }
                result = auth.register_user(
                    values["first_name"],
                    values["last_name"],
                    values["email"],
                    _form_value(fields, "password", strip=False),
                    _form_value(fields, "password_confirmation", strip=False),
                    email_notifications=_form_checked(fields, "email_notifications"),
                    telegram_notifications=_form_checked(fields, "telegram_notifications"),
                )
                if not result.ok:
                    self._send(
                        render_auth_html("register", result, values, next_path).encode("utf-8"),
                        "text/html; charset=utf-8",
                        status=400,
                    )
                    return
                if result.user and result.user["preferences"]["email"]:
                    _save_subscriber(result.user["email"])
                token, expires_at = auth.create_session(str(result.user["id"]), persistent=False)
                self._redirect(next_path, self._session_cookie_header(token, expires_at, persistent=False))
                return

            values = {"email": _form_value(fields, "email")}
            result = auth.authenticate_user(values["email"], _form_value(fields, "password", strip=False))
            if not result.ok:
                self._send(
                    render_auth_html("login", result, values, next_path).encode("utf-8"),
                    "text/html; charset=utf-8",
                    status=401,
                )
                return
            persistent = _form_checked(fields, "remember_me")
            token, expires_at = auth.create_session(str(result.user["id"]), persistent=persistent)
            self._redirect(next_path, self._session_cookie_header(token, expires_at, persistent=persistent))
            return
        if path == "/logout":
            auth.revoke_session(self._session_token())
            self._redirect("/login", self._expired_session_cookie_header())
            return
        if parsed.path == "/push/subscribe":
            if self._current_user() is None:
                self._send(
                    json.dumps(
                        {"ok": False, "message": "Accedi per attivare le notifiche push."},
                        ensure_ascii=False,
                    ).encode("utf-8"),
                    "application/json; charset=utf-8",
                    status=401,
                )
                return
            self._handle_push_subscribe()
            return
        if path not in {
            "/newsletter",
            "/notifications/suspension",
            "/settings/profile",
            "/settings/preferences",
        }:
            self.send_error(404)
            return
        user = self._require_user(self._request_path(parsed))
        if user is None:
            return
        fields = self._read_form_fields()
        if path == "/settings/profile":
            previous_email = str(user.get("email") or "")
            result = auth.update_user_email(str(user["id"]), _form_value(fields, "email"))
            current_user = result.user or user
            if result.ok:
                if current_user["preferences"].get("email"):
                    _replace_subscriber(previous_email, str(current_user["email"]))
                else:
                    _remove_subscriber(previous_email)
            value = str(current_user.get("email") or "")
        elif path == "/settings/preferences":
            current_preferences = user.get("preferences") or {}
            result = auth.update_notification_preferences(
                str(user["id"]),
                email_notifications=_form_checked(fields, "email_notifications"),
                telegram_notifications=bool(current_preferences.get("telegram", False)),
            )
            current_user = result.user or user
            if result.ok:
                if current_user["preferences"]["email"]:
                    _save_subscriber(str(current_user["email"]))
                else:
                    _remove_subscriber(str(current_user["email"]))
                _sync_account_telegram_subscription(current_user)
            value = str(current_user.get("email") or "")
        elif path == "/notifications/suspension":
            action = (fields.get("action") or ["save"])[0]
            if action == "clear":
                notifications.clear_user_notification_suspension(str(user.get("id") or ""))
                result = NewsletterResult(
                    ok=True,
                    title="Sospensione annullata",
                    message="L'invio delle notifiche per questo account Ã¨ stato riattivato.",
                )
            else:
                start_value = (fields.get("start_date") or [""])[0]
                end_value = (fields.get("end_date") or [""])[0]
                start = _parse_iso_date(start_value)
                end = _parse_iso_date(end_value)
                if start is None or end is None or end < start or start < _project_today():
                    result = NewsletterResult(
                        ok=False,
                        title="Periodo non valido",
                        message="Seleziona un intervallo valido con date non passate e fine non precedente all'inizio.",
                    )
                else:
                    notifications.save_user_notification_suspension(str(user.get("id") or ""), start, end)
                    result = NewsletterResult(
                        ok=True,
                        title="Sospensione aggiornata",
                        message="Le notifiche per questo account sono state sospese nel periodo selezionato.",
                    )
            value = ""
            current_user = user
        else:
            previous_email = str(user.get("email") or "")
            email_address = _form_value(fields, "email")
            profile_result = auth.update_user_email(str(user["id"]), email_address)
            if not profile_result.ok:
                self._send(
                    render_settings_html(profile_result, email_address, user).encode("utf-8"),
                    "text/html; charset=utf-8",
                    status=400,
                )
                return
            current_user = profile_result.user or user
            preference_result = auth.update_notification_preferences(
                str(current_user["id"]),
                email_notifications=True,
                telegram_notifications=bool(current_user["preferences"].get("telegram")),
            )
            current_user = preference_result.user or current_user
            _replace_subscriber(previous_email, str(current_user["email"]))
            result = subscribe_and_send_newsletter(email_address, user_id=str(current_user.get("id") or ""))
            value = email_address
        self._send(
            render_settings_html(result, value, current_user).encode("utf-8"),
            "text/html; charset=utf-8",
            status=200 if result.ok else 400,
        )

    def _handle_api_saved_news(self, user: dict) -> None:
        payload = self._read_json_object()
        if payload is None:
            return
        news_id = str(payload.get("news_id") or "")
        action = str(payload.get("action") or "save").casefold()
        if not re.fullmatch(r"n_[a-f0-9]{24}", news_id):
            self._send_json({"ok": False, "message": "Identificativo notizia non valido."}, status=400)
            return
        if action == "remove":
            removed = remove_saved_news_for_user(user, news_id)
            self._send_json(
                {
                    "ok": True,
                    "saved": False,
                    "news_id": news_id,
                    "message": "Notizia rimossa dai salvati." if removed else "La notizia non era salvata.",
                }
            )
            return
        if action != "save":
            self._send_json({"ok": False, "message": "Azione non supportata."}, status=400)
            return
        record = find_news_record(news_id)
        if record is None:
            self._send_json({"ok": False, "message": "Notizia non trovata."}, status=404)
            return
        created = save_news_for_user(user, record)
        self._send_json(
            {
                "ok": True,
                "saved": True,
                "item": _api_news_record(record, {news_id}),
                "message": "Notizia salvata." if created else "La notizia era già salvata.",
            }
        )

    def _handle_api_push_subscription(self, user: dict, payload: dict) -> None:
        if not public_firebase_config().get("configured"):
            self._send_json(
                {"ok": False, "message": "Le notifiche push non sono configurate per questo ambiente."}, status=409
            )
            return
        try:
            save_fcm_token(str(payload.get("token") or ""), user_id=str(user.get("id") or ""))
        except ValueError as exc:
            self._send_json({"ok": False, "message": str(exc)}, status=422)
            return
        self._send_json(
            {"ok": True, "message": "Notifiche push attivate su questo dispositivo."},
            status=201,
        )

    def _handle_api_push_status(self, user: dict, payload: dict) -> None:
        try:
            registered = fcm_token_registered_for_user(
                str(payload.get("token") or ""),
                str(user.get("id") or ""),
            )
        except ValueError as exc:
            self._send_json({"ok": False, "message": str(exc)}, status=422)
            return
        self._send_json({"ok": True, "registered": registered})

    def _handle_api_push_unsubscribe(self, user: dict, payload: dict) -> None:
        try:
            removed = remove_fcm_token(str(payload.get("token") or ""), user_id=str(user.get("id") or ""))
        except ValueError as exc:
            self._send_json({"ok": False, "message": str(exc)}, status=422)
            return
        self._send_json(
            {
                "ok": True,
                "removed": removed,
                "message": "Notifiche push disattivate su questo dispositivo." if removed else "Nessun token push attivo da rimuovere.",
            }
        )

    def _read_json_object(self, maximum_bytes: int = 32_768) -> dict | None:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if content_length <= 0 or content_length > maximum_bytes:
            self._send_json({"ok": False, "message": "Corpo della richiesta non valido."}, status=400)
            return None
        raw_body = self.rfile.read(content_length).decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            self._send_json({"ok": False, "message": "JSON non valido."}, status=400)
            return None
        if not isinstance(payload, dict):
            self._send_json({"ok": False, "message": "JSON non valido."}, status=400)
            return None
        return payload

    def _is_same_origin_request(self) -> bool:
        """Reject cross-site cookie mutations while keeping CLI clients usable."""

        origin = str(self.headers.get("Origin") or "").strip()
        if not origin:
            return True
        try:
            parsed_origin = urlparse(origin)
        except ValueError:
            return False
        host = str(self.headers.get("Host") or "").strip().casefold()
        return parsed_origin.scheme.casefold() in {"http", "https"} and parsed_origin.netloc.casefold() == host

    def _handle_saved_news(self, user: dict) -> None:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        raw_body = self.rfile.read(min(max(content_length, 0), 16_384)).decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            self._send_json({"ok": False, "message": "Richiesta non valida."}, status=400)
            return
        if not isinstance(payload, dict):
            self._send_json({"ok": False, "message": "Richiesta non valida."}, status=400)
            return
        news_id = str(payload.get("news_id") or "")
        action = str(payload.get("action") or "save")
        if action == "remove":
            removed = remove_saved_news_for_user(user, news_id)
            self._send_json(
                {
                    "ok": True,
                    "saved": False,
                    "message": "Notizia rimossa dai salvati." if removed else "La notizia non era salvata.",
                }
            )
            return
        if action != "save":
            self._send_json({"ok": False, "message": "Azione non supportata."}, status=400)
            return
        record = find_news_record(news_id)
        if record is None:
            self._send_json({"ok": False, "message": "Notizia non trovata."}, status=404)
            return
        created = save_news_for_user(user, record)
        self._send_json(
            {
                "ok": True,
                "saved": True,
                "message": "Notizia salvata." if created else "La notizia è già salvata.",
            }
        )

    def _read_form_fields(self) -> dict[str, list[str]]:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        body = self.rfile.read(min(max(content_length, 0), 16_384)).decode("utf-8", errors="replace")
        return parse_qs(body, keep_blank_values=True)

    def _send_json(self, payload: dict, status: int = 200, headers: dict[str, str] | None = None) -> None:
        response_headers = {
            "Cache-Control": "private, no-store",
            "Vary": "Cookie",
        }
        response_headers.update(headers or {})
        self._send(
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            status=status,
            headers=response_headers,
        )

    def _session_token(self) -> str | None:
        raw_cookie = self.headers.get("Cookie", "")
        if not raw_cookie:
            return None
        try:
            cookies = SimpleCookie()
            cookies.load(raw_cookie)
        except (CookieError, ValueError):
            return None
        morsel = cookies.get(SESSION_COOKIE_NAME)
        return morsel.value if morsel else None

    def _current_user(self) -> dict | None:
        return auth.get_user_for_session(self._session_token())

    def _require_user(self, next_path: str) -> dict | None:
        user = self._current_user()
        if user is not None:
            return user
        self._redirect_to_login(next_path)
        return None

    @staticmethod
    def _request_path(parsed) -> str:
        return f"{parsed.path}?{parsed.query}" if parsed.query else parsed.path

    def _redirect_to_login(self, next_path: str) -> None:
        self._redirect(f"/login?next={quote(_safe_next_path(next_path), safe='')}")

    def _redirect(self, location: str, cookie_header: str | None = None) -> None:
        safe_location = (
            location
            if location in {"/login", "/register"} or location.startswith("/login?")
            else _safe_next_path(location)
        )
        self.send_response(303)
        self.send_header("Location", safe_location)
        if cookie_header:
            self.send_header("Set-Cookie", cookie_header)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _session_cookie_header(self, token: str, expires_at: datetime, *, persistent: bool) -> str:
        cookie = SimpleCookie()
        cookie[SESSION_COOKIE_NAME] = token
        cookie[SESSION_COOKIE_NAME]["path"] = "/"
        cookie[SESSION_COOKIE_NAME]["httponly"] = True
        cookie[SESSION_COOKIE_NAME]["samesite"] = "Lax"
        if persistent:
            cookie[SESSION_COOKIE_NAME]["max-age"] = str(max(0, int((expires_at - datetime.now(expires_at.tzinfo)).total_seconds())))
            cookie[SESSION_COOKIE_NAME]["expires"] = email.utils.format_datetime(expires_at, usegmt=True)
        if self.headers.get("X-Forwarded-Proto", "").casefold() == "https":
            cookie[SESSION_COOKIE_NAME]["secure"] = True
        return cookie.output(header="").strip()

    def _expired_session_cookie_header(self) -> str:
        cookie = SimpleCookie()
        cookie[SESSION_COOKIE_NAME] = ""
        cookie[SESSION_COOKIE_NAME]["path"] = "/"
        cookie[SESSION_COOKIE_NAME]["httponly"] = True
        cookie[SESSION_COOKIE_NAME]["samesite"] = "Lax"
        cookie[SESSION_COOKIE_NAME]["max-age"] = "0"
        cookie[SESSION_COOKIE_NAME]["expires"] = "Thu, 01 Jan 1970 00:00:00 GMT"
        return cookie.output(header="").strip()

    def _handle_push_subscribe(self) -> None:
        if not public_firebase_config().get("configured"):
            self._send(
                json.dumps(
                    {"ok": False, "message": "Firebase non configurato o disattivato."},
                    ensure_ascii=False,
                ).encode("utf-8"),
                "application/json; charset=utf-8",
                status=400,
            )
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        raw_body = self.rfile.read(min(content_length, 32_768)).decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw_body)
            save_fcm_token(str(payload.get("token") or ""))
        except (json.JSONDecodeError, ValueError) as exc:
            self._send(
                json.dumps({"ok": False, "message": str(exc)}, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
                status=400,
            )
            return
        self._send(
            json.dumps({"ok": True, "message": "Notifiche push attivate."}, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            status=201,
        )

    def log_message(self, format, *args):
        return None

    def send_error(self, code, message=None, explain=None):
        """Override per evitare BrokenPipeError quando il client si disconnette."""
        try:
            super().send_error(code, message, explain)
        except (BrokenPipeError, ConnectionError, OSError):
            pass

    def _send(
        self,
        payload: bytes,
        content_type: str,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        for name, value in (headers or {}).items():
            self.send_header(str(name), str(value))
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionError, OSError):
            pass


def configure_notification_suspension(start_value: str, end_value: str) -> NewsletterResult:
    start = _parse_iso_date(str(start_value or "").strip())
    end = _parse_iso_date(str(end_value or "").strip())
    if start is None or end is None:
        return NewsletterResult(
            ok=False,
            title="Periodo non valido",
            message="Seleziona una data di inizio e una data di fine.",
        )
    today = _project_today()
    if start < today or end < today:
        return NewsletterResult(
            ok=False,
            title="Date non selezionabili",
            message="Le date passate non possono essere selezionate.",
        )
    if end < start:
        return NewsletterResult(
            ok=False,
            title="Periodo non valido",
            message="La data di fine non puo essere precedente alla data di inizio.",
        )

    suspension = notifications.save_notification_suspension(start, end)
    return NewsletterResult(
        ok=True,
        title="Sospensione aggiornata",
        message=(
            "Le notifiche sono sospese "
            f"dal {suspension.start.strftime('%d/%m/%Y')} al {suspension.end.strftime('%d/%m/%Y')}."
        ),
    )


def clear_notification_suspension() -> NewsletterResult:
    notifications.clear_notification_suspension()
    return NewsletterResult(
        ok=True,
        title="Sospensione annullata",
        message="L'invio delle notifiche e stato riattivato immediatamente.",
    )


def subscribe_and_send_newsletter(email_address: str, *, user_id: str | None = None) -> NewsletterResult:
    normalized_email = email_address.strip().lower()
    if not EMAIL_PATTERN.match(normalized_email):
        return NewsletterResult(
            ok=False,
            title="Email non valida",
            message="Inserisci un indirizzo email valido per ricevere la newsletter.",
            email=email_address,
        )

    _save_subscriber(normalized_email)
    if notifications.notifications_suspended() or (
        user_id is not None and notifications.user_notifications_suspended(user_id, now=_project_now())
    ):
        return NewsletterResult(
            ok=True,
            title="Newsletter registrata",
            message=(
                "Email registrata. Le notifiche sono attualmente sospese e non verranno inviate "
                "fino alla riattivazione automatica."
            ),
            email=normalized_email,
            delivered=False,
        )

    subject, body = _weekly_newsletter_message()
    message = _build_email_message(normalized_email, subject, body)
    if _smtp_configured():
        try:
            _send_email(message)
        except OSError as exc:
            logger.exception("Invio SMTP della newsletter non riuscito (rete); messaggio salvato in outbox")
            _save_outbox_message(message)
            err_msg = str(exc).rstrip(".")
            return NewsletterResult(
                ok=False,
                title="Server email non raggiungibile",
                message=(
                    f"Connessione SMTP non riuscita: {err_msg}. "
                    "Il messaggio è stato salvato in outbox; verifica le credenziali SMTP "
                    "nelle impostazioni del server e riprova."
                ),
                email=normalized_email,
            )
        except Exception:
            logger.exception("Invio SMTP della newsletter non riuscito; messaggio salvato in outbox")
            _save_outbox_message(message)
            return NewsletterResult(
                ok=False,
                title="Invio email non riuscito",
                message=(
                    "La richiesta è stata salvata, ma il digest non può essere inviato ora. "
                    "Riprova più tardi."
                ),
                email=normalized_email,
            )
        return NewsletterResult(
            ok=True,
            title="Newsletter inviata",
            message="Email registrata e digest settimanale inviato subito.",
            email=normalized_email,
            delivered=True,
        )

    _save_outbox_message(message)
    return NewsletterResult(
        ok=True,
        title="Newsletter pronta in outbox",
        message=(
            "Email registrata. Configura SMTP_HOST e SMTP_FROM per inviarla davvero; "
            "intanto il messaggio e stato salvato in data/newsletter_outbox."
        ),
        email=normalized_email,
        delivered=False,
    )


def start_telegram_start_poller() -> None:
    global _TELEGRAM_POLLER_STARTED
    if _TELEGRAM_POLLER_STARTED or not _telegram_configured() or not _telegram_polling_enabled():
        logger.info(
            "Telegram poller not started: started=%s configured=%s enabled=%s",
            _TELEGRAM_POLLER_STARTED,
            _telegram_configured(),
            _telegram_polling_enabled(),
        )
        return
    poll_lock = _acquire_telegram_poller_lock()
    if poll_lock is None:
        logger.warning("Telegram poller not started: another local process already owns the polling lock")
        return
    _TELEGRAM_POLLER_STARTED = True
    thread = threading.Thread(
        target=_telegram_start_polling_loop,
        args=(poll_lock,),
        name="telegram-start-poller",
        daemon=True,
    )
    thread.start()
    logger.info("Telegram poller started: interval_seconds=%s", _telegram_poll_interval())


def _connect_account_telegram_start(start: TelegramStart) -> tuple[NewsletterResult, str | None]:
    """Bind a private Telegram chat to the account named by one-use intent."""

    if not start.payload:
        return (
            NewsletterResult(
                ok=False,
                title="Collegamento Telegram non valido",
                message="Il link di collegamento non contiene un codice valido.",
            ),
            None,
        )
    user_id = auth.user_id_from_telegram_connect_payload(start.payload)
    previous_chat_id = auth.telegram_chat_id_for_user(user_id) if user_id else None
    connection = auth.connect_telegram_chat_from_payload(
        start.payload,
        start.chat_id,
        telegram_user_id=start.telegram_user_id,
        telegram_username=start.username,
        telegram_first_name=start.first_name,
        telegram_last_name=start.last_name,
    )
    if not connection.ok or not connection.user:
        return (
            NewsletterResult(
                ok=False,
                title="Collegamento Telegram non riuscito",
                message=connection.message or "Il link Telegram non è valido, è scaduto o è già stato usato.",
            ),
            None,
        )
    if previous_chat_id and previous_chat_id != start.chat_id:
        _remove_json_subscriber(TELEGRAM_SUBSCRIBERS_PATH, previous_chat_id)

    user = connection.user
    preferences = user.get("preferences") or {}
    preference_update = auth.update_notification_preferences(
        str(user["id"]),
        email_notifications=bool(preferences.get("email", True)),
        telegram_notifications=True,
    )
    if not preference_update.ok or not preference_update.user:
        return (
            NewsletterResult(
                ok=False,
                title="Collegamento Telegram non riuscito",
                message=preference_update.message or "Non riesco ad attivare Telegram per questo account.",
            ),
            None,
        )
    _sync_account_telegram_subscription(preference_update.user)
    return (
        NewsletterResult(
            ok=True,
            title="Chat Telegram collegata",
            message="Chat collegata al tuo account.",
        ),
        str(preference_update.user["id"]),
    )


def process_telegram_start_updates(updates: list[dict]) -> list[NewsletterResult]:
    state = _load_telegram_start_state()
    receipts = _initial_telegram_delivery_receipts(state)
    current_week = _telegram_week_key(_project_today())
    delivered = {
        chat_id
        for chat_id, receipt in receipts.items()
        if receipt.get("week") == current_week
    }
    pending = _pending_telegram_initial_deliveries(state)
    last_update_id = int(state.get("last_update_id") or 0)
    results: list[NewsletterResult] = []

    logger.info(
        "Telegram /start processor: updates=%s current_week=%s receipts=%s pending=%s legacy_delivered=%s",
        len(updates),
        current_week,
        len(receipts),
        len(pending),
        len(state.get("initial_digest_chat_ids") or []),
    )
    _retry_pending_telegram_initial_deliveries(pending, delivered, receipts, results)

    for update in sorted(updates, key=lambda item: int(item.get("update_id") or 0)):
        update_id = int(update.get("update_id") or 0)
        last_update_id = max(last_update_id, update_id)
        start = _telegram_start_event_from_update(update)
        if not start:
            logger.info("Telegram update ignored: update_id=%s is not a valid private /start", update_id)
            continue
        logger.info(
            "Telegram /start received: update_id=%s chat=%s telegram_user=%s payload_kind=%s",
            update_id,
            _telegram_log_identifier(start.chat_id),
            _telegram_log_identifier(start.telegram_user_id),
            "account" if start.payload and start.payload.startswith("connect_") else "generic",
        )
        _record_telegram_contact(start)
        account_user_id = None
        account_link_conflict = False
        account_payload = bool(start.payload and start.payload.casefold().startswith("connect_"))
        active_account_intent = auth.user_id_from_telegram_connect_payload(start.payload or "") if account_payload else None
        if active_account_intent:
            logger.info(
                "Telegram /start account intent resolved: chat=%s account=%s",
                _telegram_log_identifier(start.chat_id),
                _telegram_log_identifier(active_account_intent),
            )
            connection_result, account_user_id = _connect_account_telegram_start(start)
            if not connection_result.ok:
                existing_owner = auth.telegram_chat_owner_user_id(start.chat_id)
                if existing_owner and existing_owner != active_account_intent:
                    account_link_conflict = True
                    account_user_id = existing_owner
                    _save_json_subscriber(TELEGRAM_SUBSCRIBERS_PATH, start.chat_id)
                    _record_telegram_contact(start, account_user_id=existing_owner)
                    logger.warning(
                        "Telegram /start account link conflict: chat=%s remains bound to account=%s; digest delivery continues",
                        _telegram_log_identifier(start.chat_id),
                        _telegram_log_identifier(existing_owner),
                    )
                else:
                    logger.error(
                        "Telegram /start account link failed: chat=%s message=%s",
                        _telegram_log_identifier(start.chat_id),
                        connection_result.message,
                    )
                    results.append(connection_result)
                    continue
            else:
                _record_telegram_contact(start, account_user_id=account_user_id)
        elif account_payload and start.chat_id not in delivered:
            connection_result, _ = _connect_account_telegram_start(start)
            logger.error(
                "Telegram /start rejected stale account intent: chat=%s message=%s",
                _telegram_log_identifier(start.chat_id),
                connection_result.message,
            )
            results.append(connection_result)
            continue

        # A private /start is an explicit request for the current digest.
        # Delivery receipts only deduplicate the scheduled weekly send; they
        # must not suppress a user-triggered resend from either client.
        if start.chat_id in delivered:
            logger.info(
                "Telegram /start resend requested after an existing receipt: chat=%s week=%s payload_kind=%s",
                _telegram_log_identifier(start.chat_id),
                current_week,
                "account" if account_payload else "generic",
            )
        if any(item.get("chat_id") == start.chat_id for item in pending):
            results.append(
                NewsletterResult(
                    ok=True,
                    title="Invio Telegram in corso",
                    message="Il digest iniziale è già in coda per questa chat.",
                )
            )
            logger.info("Telegram /start delivery remains queued: chat=%s", _telegram_log_identifier(start.chat_id))
            continue
        if not account_payload:
            _save_json_subscriber(TELEGRAM_SUBSCRIBERS_PATH, start.chat_id)
            logger.info("Telegram user registered: chat=%s", _telegram_log_identifier(start.chat_id))

        account_suspended = bool(
            account_user_id
            and notifications.user_notifications_suspended(account_user_id, now=_project_now())
        )
        if notifications.notifications_suspended() or account_suspended:
            results.append(
                NewsletterResult(
                    ok=True,
                    title="Invio Telegram sospeso",
                    message=(
                        "Chat registrata, ma l'invio automatico del digest resta sospeso "
                        "nel periodo configurato per questo account."
                        if account_suspended
                        else "Chat registrata, ma l'invio automatico del digest resta sospeso nel periodo configurato."
                    ),
                )
            )
            logger.warning("Telegram /start delivery suspended: chat=%s", _telegram_log_identifier(start.chat_id))
            continue

        message = ""
        try:
            message = _initial_telegram_message()
            if not message.strip():
                raise RuntimeError("Il messaggio iniziale Telegram è vuoto.")
            _send_telegram_message(start.chat_id, message)
        except TelegramDeliveryError as exc:
            _log_telegram_exception(
                "Telegram /start API delivery failed: chat=%s retryable=%s blocked=%s sent_chunks=%s",
                _telegram_log_identifier(start.chat_id),
                exc.retryable,
                exc.blocked,
                exc.sent_chunks,
            )
            if exc.blocked:
                _remove_json_subscriber(TELEGRAM_SUBSCRIBERS_PATH, start.chat_id)
            else:
                pending.append(
                    {
                        "chat_id": start.chat_id,
                        "message": message or _initial_telegram_message(),
                        "next_chunk": exc.sent_chunks,
                        "attempts": 1,
                        "created_at": _utc_now_string(),
                        "last_error": _telegram_delivery_error_message(exc),
                        "account_user_id": account_user_id,
                    }
                )
            results.append(
                NewsletterResult(
                    ok=False,
                    title="Invio Telegram non riuscito",
                    message=_telegram_delivery_error_message(exc),
                )
            )
            continue
        except Exception:
            _log_telegram_exception(
                "Telegram /start digest construction failed: chat=%s; queuing a rebuild",
                _telegram_log_identifier(start.chat_id),
            )
            pending.append(
                {
                    "chat_id": start.chat_id,
                    "message": "",
                    "needs_build": True,
                    "next_chunk": 0,
                    "attempts": 1,
                    "created_at": _utc_now_string(),
                    "last_error": "Costruzione del digest iniziale non riuscita.",
                    "account_user_id": account_user_id,
                }
            )
            results.append(
                NewsletterResult(
                    ok=False,
                    title="Preparazione digest Telegram non riuscita",
                    message="Il digest è stato messo in coda e verrà ritentato automaticamente.",
                )
            )
            continue

        delivered.add(start.chat_id)
        _record_initial_telegram_delivery_receipt(receipts, start.chat_id, message, current_week)
        logger.info(
            "Telegram /start digest accepted by Telegram API: chat=%s chars=%s fingerprint=%s",
            _telegram_log_identifier(start.chat_id),
            len(message),
            _telegram_message_fingerprint(message),
        )
        results.append(
            NewsletterResult(
                ok=True,
                title="Digest iniziale inviato",
                message=(
                    "Digest settimanale inviato; la chat resta associata all'account Telegram già collegato."
                    if account_link_conflict
                    else "Benvenuto Telegram e digest settimanale inviati automaticamente."
                ),
                delivered=True,
            )
        )

    state["last_update_id"] = last_update_id
    state["initial_delivery_receipts"] = receipts
    state["initial_digest_chat_ids"] = sorted(receipts)
    state["pending_initial_deliveries"] = pending
    _save_telegram_start_state(state)
    return results


def _pending_telegram_initial_deliveries(state: dict) -> list[dict]:
    pending = state.get("pending_initial_deliveries") or []
    return [
        dict(item)
        for item in pending
        if isinstance(item, dict)
        and item.get("chat_id")
        and (item.get("message") or item.get("needs_build"))
    ]


def _initial_telegram_delivery_receipts(state: dict) -> dict[str, dict]:
    raw_receipts = state.get("initial_delivery_receipts") or {}
    if not isinstance(raw_receipts, dict):
        return {}
    return {
        str(chat_id): dict(receipt)
        for chat_id, receipt in raw_receipts.items()
        if str(chat_id).strip() and isinstance(receipt, dict) and receipt.get("week")
    }


def _record_initial_telegram_delivery_receipt(
    receipts: dict[str, dict],
    chat_id: str,
    message: str,
    week_key: str,
) -> None:
    receipts[str(chat_id)] = {
        "week": week_key,
        "sent_at": _utc_now_string(),
        "message_fingerprint": _telegram_message_fingerprint(message),
        "message_length": len(message),
    }


def _retry_pending_telegram_initial_deliveries(
    pending: list[dict],
    delivered: set[str],
    receipts: dict[str, dict],
    results: list[NewsletterResult],
) -> None:
    if notifications.notifications_suspended():
        return
    for item in list(pending):
        chat_id = str(item.get("chat_id") or "")
        if not chat_id or chat_id in delivered:
            pending.remove(item)
            continue
        account_user_id = str(item.get("account_user_id") or "")
        if account_user_id and notifications.user_notifications_suspended(account_user_id, now=_project_now()):
            # Preserve the queued delivery so it resumes automatically when
            # the account-scoped pause ends.
            continue
        try:
            if item.get("needs_build"):
                logger.info("Telegram queued initial digest: rebuilding message for chat=%s", _telegram_log_identifier(chat_id))
                item["message"] = _initial_telegram_message()
                item["needs_build"] = False
            message = str(item.get("message") or "")
            if not message.strip():
                raise RuntimeError("Il messaggio Telegram in coda è vuoto.")
            _send_telegram_message(
                chat_id,
                message,
                start_chunk=max(0, int(item.get("next_chunk") or 0)),
            )
        except TelegramDeliveryError as exc:
            _log_telegram_exception(
                "Telegram queued initial digest delivery failed: chat=%s retryable=%s blocked=%s sent_chunks=%s",
                _telegram_log_identifier(chat_id),
                exc.retryable,
                exc.blocked,
                exc.sent_chunks,
            )
            if exc.blocked:
                _remove_json_subscriber(TELEGRAM_SUBSCRIBERS_PATH, chat_id)
                pending.remove(item)
                logger.warning("Chat Telegram rimossa perché il bot è bloccato: %s", chat_id)
                continue
            item["next_chunk"] = max(int(item.get("next_chunk") or 0), exc.sent_chunks)
            item["attempts"] = int(item.get("attempts") or 0) + 1
            item["last_error"] = _telegram_delivery_error_message(exc)
            continue
        except Exception:
            item["needs_build"] = True
            item["attempts"] = int(item.get("attempts") or 0) + 1
            item["last_error"] = "Ricostruzione del digest iniziale non riuscita."
            _log_telegram_exception("Telegram queued initial digest could not be rebuilt: chat=%s", _telegram_log_identifier(chat_id))
            continue
        delivered.add(chat_id)
        _record_initial_telegram_delivery_receipt(receipts, chat_id, message, _telegram_week_key(_project_today()))
        pending.remove(item)
        logger.info("Telegram queued initial digest accepted by Telegram API: chat=%s", _telegram_log_identifier(chat_id))
        results.append(
            NewsletterResult(
                ok=True,
                title="Digest iniziale inviato",
                message="Il digest iniziale in attesa è stato inviato automaticamente.",
                delivered=True,
            )
        )


def _telegram_start_polling_loop(poll_lock=None) -> None:
    poll_lock = poll_lock or _acquire_telegram_poller_lock()
    if poll_lock is None:
        logger.warning("Polling Telegram disattivato: un altro processo locale sta gia leggendo gli aggiornamenti")
        return
    try:
        while True:
            try:
                _process_pending_telegram_start_updates()
            except TelegramPollingConflictError as exc:
                logger.error("Polling Telegram sospeso per conflitto esterno: %s", exc)
                time.sleep(max(60, _telegram_poll_interval()))
                continue
            except Exception as exc:
                _log_telegram_exception("Polling Telegram non riuscito: %s", type(exc).__name__)
            time.sleep(_telegram_poll_interval())
    finally:
        _release_telegram_poller_lock(poll_lock)


def _process_pending_telegram_start_updates(
    *, wait_seconds: int = 20, blocking: bool = True
) -> list[NewsletterResult] | None:
    """Fetch and process unhandled bot messages under one in-process lock.

    The regular poller uses long polling. The settings confirmation uses an
    immediate request so users can confirm a just-sent ``/start`` without
    waiting for the background cycle.
    """

    acquired = _TELEGRAM_UPDATES_LOCK.acquire(blocking=blocking)
    if not acquired:
        logger.info("Telegram update poll skipped because another poll is active")
        return None
    try:
        state = _load_telegram_start_state()
        last_update_id = int(state.get("last_update_id") or 0)
        updates = _telegram_updates(
            last_update_id + 1 if last_update_id else None,
            wait_seconds=wait_seconds,
        )
        logger.info(
            "Telegram updates retrieved: offset=%s count=%s",
            last_update_id + 1 if last_update_id else None,
            len(updates),
        )
        return process_telegram_start_updates(updates)
    finally:
        _TELEGRAM_UPDATES_LOCK.release()


def _telegram_updates(offset: int | None = None, *, wait_seconds: int = 20) -> list[dict]:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    timeout_seconds = max(0, int(wait_seconds))
    params: dict[str, object] = {"timeout": timeout_seconds, "allowed_updates": json.dumps(["message"])}
    if offset:
        params["offset"] = offset
    try:
        logger.info("Telegram getUpdates request: offset=%s timeout=%s", offset, timeout_seconds)
        response = httpx.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params=params,
            timeout=max(5, timeout_seconds + 5),
        )
        if getattr(response, "status_code", None) == 409:
            raise TelegramPollingConflictError(
                "Telegram sta gia servendo getUpdates a un altro processo. "
                "Arresta il secondo worker o disattiva TELEGRAM_POLLING_ENABLED nel server web."
            ) from None
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError("Telegram non Ã¨ raggiungibile per il polling degli aggiornamenti.") from exc
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Telegram ha restituito una risposta non valida.") from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        description = str(payload.get("description") or "") if isinstance(payload, dict) else ""
        if "webhook" in description.casefold():
            raise RuntimeError("Il bot ha un webhook attivo: disattivalo prima di usare il polling.")
        raise RuntimeError("Telegram ha rifiutato la lettura degli aggiornamenti del bot.")
    updates = payload.get("result")
    if not isinstance(updates, list):
        raise RuntimeError("Telegram ha restituito aggiornamenti non validi.")
    valid_updates = [update for update in updates if isinstance(update, dict)]
    logger.info("Telegram getUpdates response: offset=%s count=%s", offset, len(valid_updates))
    return valid_updates


def _telegram_poll_interval() -> int:
    try:
        return max(5, int(os.getenv("TELEGRAM_POLL_INTERVAL_SECONDS", str(TELEGRAM_POLL_INTERVAL_SECONDS))))
    except ValueError:
        return TELEGRAM_POLL_INTERVAL_SECONDS


def _telegram_polling_enabled() -> bool:
    return os.getenv("TELEGRAM_POLLING_ENABLED", "true").casefold() not in {"0", "false", "no", "off"}


def _acquire_telegram_poller_lock():
    """Acquire an OS-level lock so only one local process can call getUpdates."""

    TELEGRAM_POLL_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_file = TELEGRAM_POLL_LOCK_PATH.open("a+b")
    try:
        lock_file.seek(0)
        if not lock_file.read(1):
            lock_file.seek(0)
            lock_file.write(b"0")
            lock_file.flush()
        lock_file.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_file
    except OSError:
        lock_file.close()
        return None


def _release_telegram_poller_lock(lock_file) -> None:
    try:
        lock_file.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        lock_file.close()


def run_telegram_polling_worker() -> None:
    """Run the single long-polling worker used by multi-process deployments."""

    load_gui_env()
    configure_gui_logging()
    if not _telegram_configured():
        raise RuntimeError("TELEGRAM_BOT_TOKEN non configurato.")
    _telegram_start_polling_loop()


def _telegram_start_event_from_update(update: dict) -> TelegramStart | None:
    message = update.get("message") or {}
    text = str(message.get("text") or "").strip()
    match = re.match(r"^/start(?:@[A-Za-z0-9_]+)?(?:\s+([^\s]+))?(?:\s.*)?$", text)
    if not match:
        return None
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    if chat.get("type") != "private" or sender.get("is_bot"):
        return None
    chat_id = str(chat.get("id") or "").strip()
    telegram_user_id = str(sender.get("id") or "").strip()
    if not re.fullmatch(r"\d+", chat_id) or telegram_user_id != chat_id:
        return None
    return TelegramStart(
        chat_id=chat_id,
        telegram_user_id=telegram_user_id,
        payload=match.group(1),
        username="".join(str(sender.get("username") or "").split())[:64],
        first_name=" ".join(str(sender.get("first_name") or "").split())[:128],
        last_name=" ".join(str(sender.get("last_name") or "").split())[:128],
    )


def _telegram_start_from_update(update: dict) -> tuple[str, str | None] | None:
    start = _telegram_start_event_from_update(update)
    return (start.chat_id, start.payload) if start else None


def _start_chat_id_from_update(update: dict) -> str | None:
    start = _telegram_start_from_update(update)
    return start[0] if start else None


def _telegram_welcome_text() -> str:
    return (
        "Benvenuto nel Research Digest AI. "
        "Ti invio subito il digest della settimana corrente; poi riceverai gli aggiornamenti configurati."
    )


def _initial_telegram_message() -> str:
    """Build one deterministic onboarding message for a private Telegram chat."""

    reference_date = _project_today()
    before_generation = _current_week_digest_view(reference_date)
    logger.info(
        "Telegram initial digest lookup: week=%s entries_before_generation=%s",
        _telegram_week_key(reference_date),
        len(before_generation.entries) if before_generation else 0,
    )
    _ensure_current_week_digest()
    current_week_view = _current_week_digest_view(reference_date)
    message = f"{_telegram_welcome_text()}\n\n{_weekly_telegram_message(reference_date)}"
    logger.info(
        "Telegram initial digest constructed: week=%s entries=%s chars=%s fingerprint=%s",
        _telegram_week_key(reference_date),
        len(current_week_view.entries) if current_week_view else 0,
        len(message),
        _telegram_message_fingerprint(message),
    )
    return message


def _load_telegram_start_state() -> dict:
    if not TELEGRAM_START_STATE_PATH.exists():
        return {}
    try:
        payload = json.loads(TELEGRAM_START_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_telegram_start_state(state: dict) -> None:
    TELEGRAM_START_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = TELEGRAM_START_STATE_PATH.with_suffix(f"{TELEGRAM_START_STATE_PATH.suffix}.tmp")
    temporary_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(TELEGRAM_START_STATE_PATH)


def _utc_now_string() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _telegram_week_key(reference_date: date) -> str:
    iso_year, iso_week, _ = reference_date.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def _telegram_log_identifier(value: str | None) -> str:
    """Keep operational logs useful without exposing full chat or account IDs."""

    normalized = str(value or "").strip()
    if len(normalized) <= 4:
        return "***"
    return f"***{normalized[-4:]}"


def _telegram_message_fingerprint(message: str) -> str:
    return hashlib.sha256(str(message or "").encode("utf-8")).hexdigest()[:12]


def _log_telegram_exception(message: str, *args) -> None:
    """Write a complete, token-safe traceback for Telegram failures."""

    trace = traceback.format_exc()
    safe_trace = re.sub(r"/bot[^/\s]+/", "/bot<redacted>/", trace)
    logger.error("%s\n%s", message % args if args else message, safe_trace.rstrip())


def _record_telegram_contact(start: TelegramStart, *, account_user_id: str | None = None) -> None:
    """Persist the minimal Telegram identity needed for onboarding and attribution."""

    with _TELEGRAM_CONTACTS_LOCK:
        contacts = _load_telegram_contacts()
        existing = contacts.get(start.chat_id) or {}
        contacts[start.chat_id] = {
            "chat_id": start.chat_id,
            "telegram_user_id": start.telegram_user_id,
            "username": start.username,
            "first_name": start.first_name,
            "last_name": start.last_name,
            "first_started_at": existing.get("first_started_at") or _utc_now_string(),
            "last_started_at": _utc_now_string(),
            "start_parameter_hash": (
                hashlib.sha256(start.payload.encode("utf-8")).hexdigest() if start.payload else ""
            ),
            "account_user_id": account_user_id or existing.get("account_user_id") or "",
        }
        _save_telegram_contacts(contacts)


def _load_telegram_contacts() -> dict[str, dict]:
    if not TELEGRAM_CONTACTS_PATH.exists():
        return {}
    try:
        payload = json.loads(TELEGRAM_CONTACTS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(chat_id): record for chat_id, record in payload.items() if isinstance(record, dict)}


def _save_telegram_contacts(contacts: dict[str, dict]) -> None:
    TELEGRAM_CONTACTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = TELEGRAM_CONTACTS_PATH.with_suffix(f"{TELEGRAM_CONTACTS_PATH.suffix}.tmp")
    temporary_path.write_text(json.dumps(contacts, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary_path.replace(TELEGRAM_CONTACTS_PATH)


def _weekly_period(reference_date: date) -> tuple[date, date]:
    start = reference_date.fromisocalendar(reference_date.isocalendar().year, reference_date.isocalendar().week, 1)
    return start, reference_date


def _current_week_digest_view(reference_date: date | None = None) -> DigestView | None:
    reference_date = reference_date or _project_today()
    start, end = _weekly_period(reference_date)
    weekly_records: list[tuple[date, Path, DigestView]] = []
    for path in _collect_digest_paths(DEFAULT_DIGEST_DIRS):
        digest_date = _digest_date_from_path(path)
        if not digest_date:
            continue
        parsed_date = _parse_iso_date(digest_date)
        if parsed_date and start <= parsed_date <= end:
            try:
                view = parse_digest_markdown(path.read_text(encoding="utf-8"), path)
                weekly_records.append((parsed_date, path, view))
            except OSError:
                continue
    if weekly_records:
        weekly_records.sort(key=lambda record: (record[0], _digest_sort_timestamp(record[2], record[1])))
        latest_date, latest_path, _ = weekly_records[-1]
        return _merge_digest_views(
            [view for _, _, view in weekly_records],
            source_path=latest_path,
            # The label reflects the latest publication actually present; it
            # must never extend the collection period into future days.
            digest_date=latest_date.isoformat(),
        )
    return None


def _weekly_digest_view(reference_date: date | None = None) -> DigestView:
    """Return only the current ISO-week digest.

    Falling back to a previous week's content would make the collection period
    printed in a notification inaccurate.  An explicit empty digest is safer.
    """

    reference_date = reference_date or _project_today()
    return _current_week_digest_view(reference_date) or _empty_current_week_digest_view(reference_date)


def _empty_current_week_digest_view(reference_date: date) -> DigestView:
    start, end = _weekly_period(reference_date)
    return DigestView(
        path=None,
        title=f"AI Research Digest - settimana {start.strftime('%d/%m')} - {end.strftime('%d/%m/%Y')}",
        subtitle="Ricerca AI / modelli - 0 voci",
        warning="Nessun digest disponibile per la settimana corrente.",
        entries=[],
        footer=[],
        digest_date=end.isoformat(),
    )


def _telegram_auto_generation_enabled() -> bool:
    return os.getenv("TELEGRAM_AUTO_GENERATE_DIGEST", "true").casefold() not in {"0", "false", "no", "off"}


def _ensure_current_week_digest(reference_date: date | None = None) -> None:
    """Generate a missing current-week digest without making the chat wait forever.

    A failed generation is throttled, rather than permanently suppressing
    retries for the whole ISO week.
    """

    reference_date = reference_date or _project_today()
    current_view = _current_week_digest_view(reference_date)
    if current_view is not None:
        logger.info(
            "Telegram current-week digest found: week=%s entries=%s",
            _telegram_week_key(reference_date),
            len(current_view.entries),
        )
        return
    if not _telegram_auto_generation_enabled():
        logger.warning("Telegram current-week digest missing and automatic generation is disabled")
        return
    if not _TELEGRAM_DIGEST_GENERATION_LOCK.acquire(blocking=False):
        logger.info("Telegram current-week digest generation is already running")
        return
    try:
        state = _load_telegram_generation_state()
        iso_year, iso_week, _ = reference_date.isocalendar()
        week_key = f"{iso_year}-W{iso_week:02d}"
        if _telegram_generation_was_recently_attempted(state, week_key):
            logger.warning("Telegram current-week digest generation recently failed or is throttled: week=%s", week_key)
            return
        state["last_attempt_week"] = week_key
        state["last_attempt_at"] = _utc_now_string()
        state["last_attempt_epoch"] = time.time()
        _save_telegram_generation_state(state)
        try:
            from src.agent import run_agent

            logger.info("Telegram current-week digest missing: starting generation with %s", os.getenv("TELEGRAM_DIGEST_CONFIG_PATH", "config.yaml"))
            run_agent(
                os.getenv("TELEGRAM_DIGEST_CONFIG_PATH", "config.yaml"),
                notify_telegram=False,
                notify_email=False,
                notify_push=False,
            )
            generated_view = _current_week_digest_view(reference_date)
            logger.info(
                "Telegram current-week digest generation completed: week=%s entries=%s",
                week_key,
                len(generated_view.entries) if generated_view else 0,
            )
        except Exception:
            _log_telegram_exception("Generazione automatica del digest Telegram non riuscita")
    finally:
        _TELEGRAM_DIGEST_GENERATION_LOCK.release()


def _telegram_generation_was_recently_attempted(state: dict, week_key: str) -> bool:
    if state.get("last_attempt_week") != week_key:
        return False
    try:
        last_attempt = float(state.get("last_attempt_epoch"))
    except (TypeError, ValueError):
        return False
    return time.time() - last_attempt < TELEGRAM_GENERATION_RETRY_SECONDS


def _load_telegram_generation_state() -> dict:
    if not TELEGRAM_GENERATION_STATE_PATH.exists():
        return {}
    try:
        payload = json.loads(TELEGRAM_GENERATION_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_telegram_generation_state(state: dict) -> None:
    TELEGRAM_GENERATION_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = TELEGRAM_GENERATION_STATE_PATH.with_suffix(
        f"{TELEGRAM_GENERATION_STATE_PATH.suffix}.tmp"
    )
    temporary_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary_path.replace(TELEGRAM_GENERATION_STATE_PATH)


def _clean_newsletter_text(value: object) -> str:
    """Remove quoted-text labels accidentally imported into newsletter content."""

    text = str(value or "")
    text = re.sub(
        r"(?i)(?:mostra\s+testo\s+citato|show\s+quoted\s+text)",
        "",
        text,
    )
    text = re.sub(r"[ \t]+(?=\n|$)", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _newsletter_message_token() -> str:
    """Create a unique value so mail clients do not collapse repeated sends."""

    seed = f"{time.time_ns()}-{threading.get_ident()}-{os.getpid()}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def _newsletter_period(view: DigestView, reference_date: date) -> tuple[date, date]:
    """Use the same ISO-week interval displayed by the homepage."""

    return _iso_week_bounds(str(view.digest_date or "")) or _weekly_period(reference_date)


def _newsletter_homepage_items(view: DigestView) -> list[dict]:
    """Return exactly the records rendered by the homepage API."""

    return list(_api_digest_payload(view).get("items") or [])


def _weekly_newsletter_message(reference_date: date | None = None, channel: str = "email") -> tuple[str, str]:
    reference_date = reference_date or _project_today()
    view = load_current_digest()
    period_start, period_end = _newsletter_period(view, reference_date)
    items = _newsletter_homepage_items(view)

    subject = NEWSLETTER_SUBJECT
    lines = [
        (
            "Le novità AI della settimana: "
            f"dal {period_start.strftime('%d/%m/%Y')} al {period_end.strftime('%d/%m/%Y')}."
        ),
        "",
    ]
    if view.warning:
        lines.extend([f"Nota: {view.warning}", ""])
    if not items:
        lines.append("Nessuna notizia disponibile nel digest selezionato.")
    for index, item in enumerate(items, 1):
        lines.extend(
            [
                f"{index}. {_clean_newsletter_text(item.get('title')) or 'Senza titolo'}",
                f"Fonte: {_clean_newsletter_text(item.get('source')) or 'Fonte non disponibile'}",
                f"Categoria: {_clean_newsletter_text(item.get('category')) or 'Non classificata'}",
                "",
                _clean_newsletter_text(item.get("summary")),
                "",
                f"Perché conta: {_clean_newsletter_text(item.get('why'))}",
                "",
                f"Link: {_clean_newsletter_text(item.get('url'))}",
                "",
            ]
        )
        if index < len(items):
            lines.extend(["------------------------------------------------------------", ""])
    if channel == "telegram":
        lines.append(
            "Ricevi questo messaggio perché hai richiesto gli aggiornamenti dal bot Telegram del Research Digest Agent."
        )
    else:
        lines.extend(["", "Ricevi questa email perché ti sei iscritto alla newsletter di Nuvia.ai"])
    return subject, "\n".join(lines).rstrip()


def _weekly_newsletter_html(reference_date: date | None = None) -> str:
    reference_date = reference_date or _project_today()
    view = load_current_digest()
    period_start, period_end = _newsletter_period(view, reference_date)
    items = _newsletter_homepage_items(view)

    parts = [
        '<html><body style="margin:0;padding:0;background:#ffffff;color:#26344f;font-family:Arial,Helvetica,sans-serif;">',
        '<main style="max-width:680px;margin:0 auto;padding:28px 24px 32px;line-height:1.58;font-size:15px;">',
        (
            '<p style="margin:0 0 30px;font-weight:700;">'
            'Le novità AI della settimana: '
            f'dal {period_start.strftime("%d/%m/%Y")} al {period_end.strftime("%d/%m/%Y")}.'
            '</p>'
        ),
    ]
    if view.warning:
        parts.append(f'<p style="margin:0 0 26px;">Nota: {html.escape(str(view.warning))}</p>')
    if not items:
        parts.append('<p style="margin:0;">Nessuna notizia disponibile nel digest selezionato.</p>')

    for index, item in enumerate(items, 1):
        title = html.escape(_clean_newsletter_text(item.get("title")) or "Senza titolo")
        source = html.escape(_clean_newsletter_text(item.get("source")) or "Fonte non disponibile")
        category = html.escape(_clean_newsletter_text(item.get("category")) or "Non classificata")
        summary = html.escape(_clean_newsletter_text(item.get("summary"))).replace("\n", "<br>")
        why = html.escape(_clean_newsletter_text(item.get("why"))).replace("\n", "<br>")
        url = _safe_public_url(item.get("url"))
        link_html = (
            f'<a href="{html.escape(url, quote=True)}" style="color:#526a9a;text-decoration:underline;">{html.escape(url)}</a>'
            if url else ""
        )
        parts.extend(
            [
                '<article style="margin:0;padding:0 0 32px;">',
                f'<p style="margin:0 0 16px;font-weight:700;">{index}. {title}</p>',
                f'<p style="margin:0 0 8px;"><strong>Fonte:</strong> {source}</p>',
                f'<p style="margin:0 0 18px;"><strong>Categoria:</strong> {category}</p>',
                f'<p style="margin:0 0 18px;">{summary}</p>',
                f'<p style="margin:0 0 18px;"><strong>Perché conta:</strong> {why}</p>',
                f'<p style="margin:0;"><strong>Link:</strong> {link_html}</p>',
                '</article>',
            ]
        )
        if index < len(items):
            parts.append(
                '<div aria-hidden="true" style="height:1px;background:#dfe5f0;margin:0 0 32px;"></div>'
            )
    parts.extend(
        [
            '<p style="margin:18px 0 0;">Ricevi questa email perché ti sei iscritto alla newsletter di Nuvia.ai</p>',
            '</main></body></html>',
        ]
    )
    return "".join(parts)


def _weekly_telegram_message(reference_date: date | None = None) -> str:
    reference_date = reference_date or _project_today()
    start, end = _weekly_period(reference_date)
    view = _current_week_digest_view(reference_date) or _empty_current_week_digest_view(reference_date)
    return _build_structured_telegram_message(view, start, end)


def send_weekly_newsletter_to_subscribers(
    reference_date: date | None = None,
    *,
    force: bool = False,
) -> dict[str, int]:
    """Send one ISO-week newsletter to every active email subscriber.

    Successful sends are recorded per recipient and ISO week.  A retry of the
    scheduled job therefore cannot duplicate a message, while failed messages
    remain eligible for a retry during the same scheduled publication window.
    """

    reference_date = reference_date or _project_today()
    recipients = _load_json_subscribers(NEWSLETTER_SUBSCRIBERS_PATH)
    summary = {"sent": 0, "failed": 0, "skipped": 0}
    if not recipients:
        return summary
    if notifications.notifications_suspended():
        summary["skipped"] = len(recipients)
        return summary
    if not force and reference_date.weekday() != _weekly_delivery_day():
        summary["skipped"] = len(recipients)
        return summary
    if not _smtp_configured():
        logger.error("Invio newsletter settimanale non eseguito: SMTP_HOST e SMTP_FROM non sono configurati")
        summary["failed"] = len(recipients)
        return summary
    if not _NEWSLETTER_DELIVERY_LOCK.acquire(blocking=False):
        summary["skipped"] = len(recipients)
        return summary

    try:
        iso_year, iso_week, _ = reference_date.isocalendar()
        week_key = f"{iso_year}-W{iso_week:02d}"
        state = _load_newsletter_delivery_state()
        delivered_by_email = state.get("weekly_delivery_by_email") or {}
        if not isinstance(delivered_by_email, dict):
            delivered_by_email = {}
        subject, body = _weekly_newsletter_message(reference_date)

        for email_address in recipients:
            if not force and delivered_by_email.get(email_address) == week_key:
                summary["skipped"] += 1
                continue
            user_id = auth.user_id_for_email(email_address)
            if user_id and notifications.user_notifications_suspended(user_id, now=_project_now()):
                summary["skipped"] += 1
                continue
            message = _build_email_message(email_address, subject, body)
            try:
                _send_email(message)
            except Exception:
                # Keep the MIME message for inspection or manual recovery.  It
                # is intentionally not marked delivered, so a same-week retry
                # may still send it after the SMTP fault is fixed.
                logger.exception("Invio newsletter settimanale non riuscito per un iscritto")
                _save_outbox_message(message)
                summary["failed"] += 1
                continue
            if not force:
                delivered_by_email[email_address] = week_key
            summary["sent"] += 1

        if not force:
            state["weekly_delivery_by_email"] = delivered_by_email
            _save_newsletter_delivery_state(state)
        return summary
    finally:
        _NEWSLETTER_DELIVERY_LOCK.release()


def send_weekly_telegram_digest_to_subscribers(
    reference_date: date | None = None,
    *,
    force: bool = False,
) -> dict[str, int]:
    """Deliver one aggregated ISO-week digest to every active Telegram chat.

    The scheduler invokes this after producing a digest.  It sends once per
    chat/week (Friday by default) and keeps the delivery state on the same
    persistent volume as the subscriber store.
    """

    reference_date = reference_date or _project_today()
    summary = {"sent": 0, "failed": 0, "skipped": 0}
    if not _telegram_configured() or notifications.notifications_suspended():
        return summary
    if not force and reference_date.weekday() != _weekly_delivery_day():
        return summary
    if not _TELEGRAM_DELIVERY_LOCK.acquire(blocking=False):
        summary["skipped"] += len(_load_json_subscribers(TELEGRAM_SUBSCRIBERS_PATH))
        return summary
    try:
        iso_year, iso_week, _ = reference_date.isocalendar()
        week_key = f"{iso_year}-W{iso_week:02d}"
        state = _load_telegram_delivery_state()
        delivered_by_chat = state.get("weekly_delivery_by_chat") or {}
        if not isinstance(delivered_by_chat, dict):
            delivered_by_chat = {}
        message = _weekly_telegram_message(reference_date)
        recipients = _load_json_subscribers(TELEGRAM_SUBSCRIBERS_PATH)
        for index, chat_id in enumerate(recipients):
            if not force and delivered_by_chat.get(chat_id) == week_key:
                summary["skipped"] += 1
                continue
            account_user_id = auth.telegram_chat_owner_user_id(chat_id)
            if account_user_id and notifications.user_notifications_suspended(account_user_id, now=_project_now()):
                summary["skipped"] += 1
                continue
            try:
                _send_telegram_message(chat_id, message)
            except TelegramDeliveryError as exc:
                summary["failed"] += 1
                if exc.blocked:
                    _remove_json_subscriber(TELEGRAM_SUBSCRIBERS_PATH, chat_id)
                    delivered_by_chat.pop(chat_id, None)
                logger.warning(
                    "Invio digest settimanale Telegram non riuscito per %s: %s",
                    chat_id,
                    _telegram_delivery_error_message(exc),
                )
                continue
            if not force:
                delivered_by_chat[chat_id] = week_key
            summary["sent"] += 1
            if index + 1 < len(recipients):
                _telegram_broadcast_sleep()
        if not force:
            state["weekly_delivery_by_chat"] = delivered_by_chat
            _save_telegram_delivery_state(state)
        return summary
    finally:
        _TELEGRAM_DELIVERY_LOCK.release()


def _telegram_weekly_delivery_day() -> int:
    """Backward-compatible alias for the common scheduled delivery day."""

    return _weekly_delivery_day()


def _weekly_delivery_day() -> int:
    """Read the one publication day shared by email and Telegram."""

    try:
        config = load_config(os.getenv("RESEARCH_DIGEST_CONFIG_PATH", "config.yaml"))
        schedule = config.get("schedule", {}) if isinstance(config, dict) else {}
        raw_value = schedule.get("weekly_delivery_day", 4) if isinstance(schedule, dict) else 4
        return min(6, max(0, int(raw_value)))
    except (FileNotFoundError, TypeError, ValueError):
        return 4


def _telegram_broadcast_sleep() -> None:
    try:
        delay = max(0.0, min(1.0, float(os.getenv("TELEGRAM_BROADCAST_DELAY_SECONDS", str(TELEGRAM_BROADCAST_DELAY_SECONDS)))))
    except ValueError:
        delay = TELEGRAM_BROADCAST_DELAY_SECONDS
    if delay:
        time.sleep(delay)


def _load_telegram_delivery_state() -> dict:
    if not TELEGRAM_DELIVERY_STATE_PATH.exists():
        return {}
    try:
        payload = json.loads(TELEGRAM_DELIVERY_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_newsletter_delivery_state() -> dict:
    if not NEWSLETTER_DELIVERY_STATE_PATH.exists():
        return {}
    try:
        payload = json.loads(NEWSLETTER_DELIVERY_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_newsletter_delivery_state(state: dict) -> None:
    NEWSLETTER_DELIVERY_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = NEWSLETTER_DELIVERY_STATE_PATH.with_suffix(
        f"{NEWSLETTER_DELIVERY_STATE_PATH.suffix}.tmp"
    )
    temporary_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary_path.replace(NEWSLETTER_DELIVERY_STATE_PATH)


def _save_telegram_delivery_state(state: dict) -> None:
    TELEGRAM_DELIVERY_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = TELEGRAM_DELIVERY_STATE_PATH.with_suffix(f"{TELEGRAM_DELIVERY_STATE_PATH.suffix}.tmp")
    temporary_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary_path.replace(TELEGRAM_DELIVERY_STATE_PATH)


def _build_structured_telegram_message(
    view: DigestView,
    start: date,
    end: date,
) -> str:
    separator = "━━━━━━━━━━━━━━━━━━━━"
    entry_label = "voce" if len(view.entries) == 1 else "voci"
    lines = [
        "🤖 <b>Research Digest AI</b>",
        f"<b>Settimana {start.strftime('%d/%m')} - {end.strftime('%d/%m/%Y')}</b>",
        "",
        f"📚 Ricerca AI / Modelli ({len(view.entries)} {entry_label})",
        "",
        separator,
    ]
    if not view.entries:
        lines.extend(["", "Nessuna notizia disponibile nella settimana corrente.", "", separator])
    for index, entry in enumerate(view.entries, 1):
        meta = _telegram_entry_meta(entry.get("meta", ""))
        lines.extend(
            [
                "",
                f"{_number_emoji(index)} <b>{_telegram_html(entry.get('title', 'Senza titolo'))}</b>",
                "",
                f"📅 <b>Data:</b> {_telegram_html(meta['date'])}",
                f"📖 <b>Fonte:</b> {_telegram_html(meta['source'])}",
                f"🏷️ <b>Categoria:</b> {_telegram_html(meta['category'])}",
                f"⭐ <b>Rilevanza:</b> {_telegram_html(meta['relevance'])}",
                "",
                _telegram_html(entry.get("summary", "")),
                "",
                "💡 <b>Perché conta:</b>",
                "",
                _telegram_html(entry.get("why", "")),
                "",
                f"🔗 {_telegram_html(entry.get('url', ''))}",
                "",
                separator,
            ]
        )
    lines.extend(
        [
            "",
            "Ricevi questo messaggio perché hai richiesto il Research Digest AI dal sito del Research Digest Agent.",
        ]
    )
    return "\n".join(lines).strip()


def _telegram_entry_meta(meta: str) -> dict[str, str]:
    text = str(meta or "")
    return {
        "source": _meta_field(text, "Fonte") or "Fonte non disponibile",
        "date": _meta_field(text, "Data") or "Data non disponibile",
        "category": _meta_field(text, "Categoria") or "categoria_non_disponibile",
        "relevance": _meta_field(text, "Rilevanza") or "n/d",
    }


def _meta_field(meta: str, label: str) -> str:
    pattern = rf"{re.escape(label)}:\s*(.*?)(?:\s+-\s+(?:Fonte|Data|Categoria|Rilevanza):|$)"
    match = re.search(pattern, meta)
    return " ".join(match.group(1).split()) if match else ""


def _telegram_html(value: str) -> str:
    return html.escape(str(value or "").strip(), quote=False)


def _number_emoji(value: int) -> str:
    digits = {
        "0": "0️⃣",
        "1": "1️⃣",
        "2": "2️⃣",
        "3": "3️⃣",
        "4": "4️⃣",
        "5": "5️⃣",
        "6": "6️⃣",
        "7": "7️⃣",
        "8": "8️⃣",
        "9": "9️⃣",
    }
    return "".join(digits.get(char, char) for char in str(value))


def _build_email_message(to_email: str, subject: str, body: str) -> EmailMessage:
    message = EmailMessage()
    sender = os.getenv("SMTP_FROM", "digest-agent@localhost")
    token = _newsletter_message_token()
    domain = sender.rsplit("@", 1)[-1] if "@" in sender else "localhost"

    message["Subject"] = subject
    message["From"] = sender
    message["To"] = to_email
    message["Date"] = email.utils.format_datetime(datetime.now().astimezone())
    message["Message-ID"] = email.utils.make_msgid(idstring=token, domain=domain)
    # Commonly used by bulk senders to prevent Gmail from collapsing repeated
    # newsletter content into its “Mostra testo citato” control.
    message["X-Entity-Ref-ID"] = token

    message.set_content(_clean_newsletter_text(body))
    if subject == NEWSLETTER_SUBJECT:
        html_body = _weekly_newsletter_html()
        unique_marker = (
            f'<span aria-hidden="true" style="display:none!important;max-height:0;overflow:hidden;'
            f'opacity:0;color:transparent;">{token}</span>'
        )
        html_body = html_body.replace("<body ", unique_marker + "<body ", 1)
        message.add_alternative(html_body, subtype="html")
    return message


def _telegram_digest_text(subject: str, body: str) -> str:
    normalized_body = str(body or "").strip()
    normalized_subject = str(subject or "").strip()
    if normalized_subject and normalized_body.startswith(normalized_subject):
        return normalized_body
    return f"{normalized_subject}\n\n{normalized_body}".strip()


def _smtp_configured() -> bool:
    return bool(os.getenv("SMTP_HOST") and os.getenv("SMTP_FROM"))


def _telegram_configured() -> bool:
    return bool(os.getenv("TELEGRAM_BOT_TOKEN"))


def _telegram_bot_username() -> str:
    username = str(os.getenv("TELEGRAM_BOT_USERNAME") or "").strip().lstrip("@")
    return username if re.fullmatch(r"[A-Za-z0-9_]{5,32}", username) else ""


def _telegram_onboarding_ready() -> bool:
    return bool(_telegram_configured() and _telegram_bot_username())


def _telegram_link_pair(payload: str | None = None) -> tuple[str, str] | None:
    """Return native and HTTPS Telegram links for the configured bot."""

    username = _telegram_bot_username()
    if not username or not _telegram_configured():
        return None
    encoded_username = quote(username, safe="_")
    suffix = f"?start={quote(payload, safe='_')}" if payload else ""
    return (
        f"tg://resolve?domain={encoded_username}{'&start=' + quote(payload, safe='_') if payload else ''}",
        f"https://t.me/{encoded_username}{suffix}",
    )


def _telegram_account_link_pair(user: dict | None) -> tuple[str, str] | None:
    """Return account-bound Telegram links for one signed-in user."""

    if not user:
        return None
    payload = auth.telegram_connect_payload(str(user.get("id") or ""))
    return _telegram_link_pair(payload) if payload else None


def _render_telegram_open_link(
    native_url: str,
    fallback_url: str,
    *,
    status_url: str | None = None,
) -> str:
    """Render an HTTPS-first control with a progressive native-launch enhancement."""

    return (
        '<div class="telegram-launch">'
        f'<a class="telegram-link" href="{html.escape(fallback_url, quote=True)}" '
        f'data-telegram-native="{html.escape(native_url, quote=True)}" '
        f'data-telegram-fallback="{html.escape(fallback_url, quote=True)}" '
        f'data-telegram-status-url="{html.escape(status_url or "", quote=True)}" '
        'aria-label="Apri il bot Telegram" aria-describedby="telegram-launch-status">Apri Telegram</a>'
        '<span class="telegram-launch-status" id="telegram-launch-status" role="status" aria-live="polite"></span>'
        "</div>"
    )


def _render_telegram_open_script() -> str:
    """Attempt Telegram's protocol handler, then safely fall back to HTTPS."""

    return """<script>
(() => {
  let activeLaunch;

  const environment = () => {
    const uaData = navigator.userAgentData;
    const platform = String(uaData?.platform || navigator.platform || "");
    const coarsePointer = window.matchMedia?.("(pointer: coarse)").matches === true;
    const touch = Number(navigator.maxTouchPoints || 0) > 0;
    const userAgent = navigator.userAgent || "";
    const appleTouchDevice = platform === "MacIntel" && touch;
    const mobile = Boolean(uaData?.mobile) || appleTouchDevice || (coarsePointer && touch) || /Android|iPhone|iPad|iPod/i.test(userAgent);
    const embedded = window.top !== window.self || Boolean(window.Telegram?.WebApp?.initData);
    return { mobile, embedded };
  };

  const statusFor = (link) => link.parentElement?.querySelector(".telegram-launch-status");
  const announce = (link, message) => {
    const status = statusFor(link);
    if (status) status.textContent = message;
  };
  const clearActiveLaunch = () => {
    if (!activeLaunch) return;
    window.clearTimeout(activeLaunch.timer);
    document.removeEventListener("visibilitychange", activeLaunch.onVisibilityChange);
    window.removeEventListener("pagehide", activeLaunch.onPageHide);
    window.removeEventListener("blur", activeLaunch.onBlur);
    activeLaunch = undefined;
  };

  document.querySelectorAll("[data-telegram-native]").forEach((link) => {
    link.addEventListener("click", (event) => {
      if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      const nativeUrl = link.dataset.telegramNative;
      const fallback = link.dataset.telegramFallback || link.href;
      if (!nativeUrl || !fallback) return;

      const device = environment();
      if (device.embedded) {
        announce(link, "Apertura di Telegram…");
        return;
      }

      event.preventDefault();
      monitorConnection(link);
      clearActiveLaunch();
      let clientOpened = false;
      const markClientOpened = () => {
        clientOpened = true;
        clearActiveLaunch();
      };
      const onVisibilityChange = () => {
        if (document.visibilityState === "hidden") markClientOpened();
      };
      const onPageHide = () => markClientOpened();
      let blurredAt = 0;
      const onBlur = () => { blurredAt = Date.now(); };
      const timeout = device.mobile ? 2400 : 3000;

      announce(link, "Apertura di Telegram…");
      document.addEventListener("visibilitychange", onVisibilityChange);
      window.addEventListener("pagehide", onPageHide, { once: true });
      window.addEventListener("blur", onBlur, { once: true });
      activeLaunch = {
        onVisibilityChange,
        onPageHide,
        onBlur,
        timer: window.setTimeout(() => {
          if (clientOpened) return;
          if (blurredAt && !document.hasFocus()) {
            clearActiveLaunch();
            link.dataset.telegramNative = "";
            announce(link, "Telegram dovrebbe essere aperto. Se non compare, seleziona di nuovo il pulsante per usare il Web.");
            return;
          }
          clearActiveLaunch();
          announce(link, "Telegram non è stato aperto: passo alla versione Web.");
          window.location.assign(fallback);
        }, timeout)
      };
      window.location.assign(nativeUrl);
    });
  });

  const monitorConnection = (link) => {
    const statusUrl = link.dataset.telegramStatusUrl;
    if (!statusUrl || !window.fetch) return;
    let attempts = 0;
    const check = async () => {
      attempts += 1;
      try {
        const response = await fetch(statusUrl, { credentials: "same-origin", cache: "no-store" });
        const payload = response.ok ? await response.json() : null;
        if (payload?.connected) {
          announce(link, "Telegram collegato: il digest della settimana corrente è stato inviato.");
          return;
        }
      } catch (_) {
        // The native launch remains usable if the optional status endpoint is unavailable.
      }
      if (attempts < 12) window.setTimeout(check, 3000);
    };
    window.setTimeout(check, 1500);
  };
})();
</script>"""


def _send_email(message: EmailMessage) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.getenv("SMTP_PORT", "587"))
    username = _smtp_username(host)
    password = os.getenv("SMTP_PASSWORD")
    use_tls = os.getenv("SMTP_USE_TLS", "true").casefold() not in {"0", "false", "no"}

    # Pre-flight: verifica rapida di raggiungibilità per fallire subito
    # su errori di rete invece di attendere il timeout SMTP.
    _smtp_check_reachable(host, port)

    with smtplib.SMTP(host, port, timeout=20) as smtp:
        if use_tls:
            smtp.starttls()
        if username and password:
            smtp.login(username, password)
        smtp.send_message(message)


def _smtp_check_reachable(host: str, port: int) -> None:
    """Solleva OSError subito se host:port non è raggiungibile."""
    import socket as _socket

    try:
        addr = _socket.getaddrinfo(host, port, _socket.AF_UNSPEC, _socket.SOCK_STREAM)
    except _socket.gaierror as exc:
        raise OSError(f"SMTP host irrisolvibile: {host}:{port} ({exc.args[1]})") from exc
    family, type_, proto, _cname, sockaddr = addr[0]
    sock = _socket.socket(family, type_, proto)
    sock.settimeout(5.0)
    try:
        sock.connect(sockaddr)
    finally:
        sock.close()


def _smtp_username(host: str) -> str | None:
    username = os.getenv("SMTP_USERNAME")
    from_address = os.getenv("SMTP_FROM")
    if "gmail.com" in host.casefold() and from_address and (not username or "@" not in username):
        return from_address
    return username or from_address


def _save_subscriber(email_address: str) -> None:
    _save_json_subscriber(NEWSLETTER_SUBSCRIBERS_PATH, email_address)


def _remove_subscriber(email_address: str) -> None:
    _remove_json_subscriber(NEWSLETTER_SUBSCRIBERS_PATH, email_address)


def _replace_subscriber(previous_email: str, current_email: str) -> None:
    previous = str(previous_email or "").strip().casefold()
    current = str(current_email or "").strip().casefold()
    if previous == current:
        if current:
            _save_subscriber(current)
        return
    with _SUBSCRIBERS_LOCK:
        subscribers = _load_json_subscribers(NEWSLETTER_SUBSCRIBERS_PATH)
        if previous:
            subscribers = [value for value in subscribers if value != previous]
        if current and current not in subscribers:
            subscribers.append(current)
        _write_json_subscribers(NEWSLETTER_SUBSCRIBERS_PATH, subscribers)


def _save_json_subscriber(path: Path, value: str) -> None:
    normalized_value = str(value or "").strip().casefold()
    if not normalized_value:
        return
    with _SUBSCRIBERS_LOCK:
        subscribers = _load_json_subscribers(path)
        if normalized_value not in subscribers:
            subscribers.append(normalized_value)
        _write_json_subscribers(path, subscribers)


def _remove_json_subscriber(path: Path, value: str) -> None:
    normalized_value = str(value or "").strip().casefold()
    if not normalized_value:
        return
    with _SUBSCRIBERS_LOCK:
        subscribers = _load_json_subscribers(path)
        filtered = [item for item in subscribers if item != normalized_value]
        if len(filtered) != len(subscribers):
            _write_json_subscribers(path, filtered)


def _sync_account_telegram_subscription(user: dict | None) -> str | None:
    """Reflect an account's Telegram toggle in the delivery subscriber list."""

    if not user:
        return None
    chat_id = auth.telegram_chat_id_for_user(str(user.get("id") or ""))
    if not chat_id:
        return None
    preferences = user.get("preferences") or {}
    if preferences.get("telegram"):
        _save_json_subscriber(TELEGRAM_SUBSCRIBERS_PATH, chat_id)
    else:
        _remove_json_subscriber(TELEGRAM_SUBSCRIBERS_PATH, chat_id)
    return chat_id


def _load_json_subscribers(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(values, list):
        return []
    return sorted({str(value).strip().casefold() for value in values if str(value).strip()})


def _write_json_subscribers(path: Path, subscribers: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(sorted(set(subscribers)), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _save_outbox_message(message: EmailMessage) -> Path:
    NEWSLETTER_OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    safe_to = re.sub(r"[^a-z0-9_.-]+", "_", str(message["To"]).casefold())
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = NEWSLETTER_OUTBOX_DIR / f"{timestamp}_{safe_to}.eml"
    path.write_text(message.as_string(), encoding="utf-8")
    return path


def _send_telegram_message(chat_id: str, text: str, *, start_chunk: int = 0) -> int:
    """Send a formatted Telegram message with bounded retries for transient API errors."""

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chunks = _telegram_chunks(text)
    sent_chunks = max(0, min(int(start_chunk), len(chunks)))
    retry_limit = _telegram_send_max_retries()
    logger.info(
        "Telegram send prepared: chat=%s chunks=%s resume_from=%s chars=%s fingerprint=%s",
        _telegram_log_identifier(chat_id),
        len(chunks),
        sent_chunks,
        len(text),
        _telegram_message_fingerprint(text),
    )
    with httpx.Client(timeout=20) as client:
        for chunk_index, chunk in enumerate(chunks[sent_chunks:], start=sent_chunks + 1):
            for attempt in range(retry_limit):
                try:
                    logger.info(
                        "Telegram sendMessage request: chat=%s chunk=%s/%s attempt=%s chars=%s",
                        _telegram_log_identifier(chat_id),
                        chunk_index,
                        len(chunks),
                        attempt + 1,
                        len(chunk),
                    )
                    response = client.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={
                            "chat_id": chat_id,
                            "text": chunk,
                            "parse_mode": "HTML",
                            "disable_web_page_preview": True,
                        },
                    )
                    payload = _raise_for_telegram_api_error(response, sent_chunks=sent_chunks)
                    result = payload.get("result") if isinstance(payload, dict) else {}
                    logger.info(
                        "Telegram sendMessage response: chat=%s chunk=%s/%s status=%s message_id=%s api_chat=%s",
                        _telegram_log_identifier(chat_id),
                        chunk_index,
                        len(chunks),
                        getattr(response, "status_code", None),
                        (result or {}).get("message_id") if isinstance(result, dict) else None,
                        _telegram_log_identifier(str((result or {}).get("chat", {}).get("id") or ""))
                        if isinstance(result, dict)
                        else "***",
                    )
                except TelegramDeliveryError as exc:
                    exc.sent_chunks = sent_chunks
                    _log_telegram_exception(
                        "Telegram sendMessage API error: chat=%s chunk=%s/%s attempt=%s retryable=%s blocked=%s retry_after=%s",
                        _telegram_log_identifier(chat_id),
                        chunk_index,
                        len(chunks),
                        attempt + 1,
                        exc.retryable,
                        exc.blocked,
                        exc.retry_after,
                    )
                    if not exc.retryable or attempt + 1 >= retry_limit:
                        raise
                    _telegram_retry_sleep(exc.retry_after or 2**attempt)
                    continue
                except httpx.HTTPError as exc:
                    _log_telegram_exception(
                        "Telegram sendMessage transport error: chat=%s chunk=%s/%s attempt=%s",
                        _telegram_log_identifier(chat_id),
                        chunk_index,
                        len(chunks),
                        attempt + 1,
                    )
                    delivery_error = TelegramDeliveryError(
                        "Connessione a Telegram non riuscita.",
                        retryable=True,
                        sent_chunks=sent_chunks,
                    )
                    if attempt + 1 >= retry_limit:
                        raise delivery_error from exc
                    _telegram_retry_sleep(2**attempt)
                    continue
                sent_chunks += 1
                break
    return sent_chunks


def _raise_for_telegram_api_error(response, *, sent_chunks: int) -> dict:
    try:
        payload = response.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    status_code = int(getattr(response, "status_code", 0) or 0)
    if payload.get("ok") is True and status_code < 400:
        return payload
    description = str(payload.get("description") or "Risposta Telegram non valida")
    parameters = payload.get("parameters") if isinstance(payload.get("parameters"), dict) else {}
    retry_after = parameters.get("retry_after")
    try:
        retry_after = max(1, int(retry_after)) if retry_after is not None else None
    except (TypeError, ValueError):
        retry_after = None
    normalized_description = description.casefold()
    blocked = status_code in {400, 403} and any(
        marker in normalized_description
        for marker in ("blocked", "deactivated", "forbidden", "chat not found")
    )
    retryable = status_code == 429 or status_code >= 500 or retry_after is not None
    raise TelegramDeliveryError(
        description,
        retryable=retryable,
        blocked=blocked,
        retry_after=retry_after,
        sent_chunks=sent_chunks,
    )


def _telegram_send_max_retries() -> int:
    try:
        return max(1, min(5, int(os.getenv("TELEGRAM_SEND_MAX_RETRIES", str(TELEGRAM_SEND_MAX_RETRIES)))))
    except ValueError:
        return TELEGRAM_SEND_MAX_RETRIES


def _telegram_retry_sleep(seconds: int) -> None:
    time.sleep(max(0, min(60, int(seconds))))


def _telegram_delivery_error_message(error: TelegramDeliveryError) -> str:
    if error.blocked:
        return "Il bot è stato bloccato o la chat non è più disponibile: gli invii sono stati sospesi per questa chat."
    if error.retry_after:
        return "Telegram sta limitando temporaneamente gli invii: il digest verrà ritentato automaticamente."
    if error.retryable:
        return "Telegram non è temporaneamente raggiungibile: il digest verrà ritentato automaticamente."
    return "Telegram non ha accettato l'invio del digest. Riprova ad avviare il bot da Telegram."


def _telegram_chunks(text: str) -> list[str]:
    normalized = str(text or "").strip()
    if not normalized:
        return [""]
    if len(normalized) <= TELEGRAM_MESSAGE_LIMIT:
        return [normalized]

    # A Telegram HTML entity or opening tag must never be cut in half.  For a
    # very long digest we intentionally fall back to plain text rather than
    # risking invalid HTML in a later chunk.
    normalized = re.sub(r"</?b>", "", normalized)
    chunks: list[str] = []
    while len(normalized) > TELEGRAM_MESSAGE_LIMIT:
        split_at = normalized.rfind("\n\n", 0, TELEGRAM_MESSAGE_LIMIT)
        if split_at < TELEGRAM_MESSAGE_LIMIT // 2:
            split_at = normalized.rfind("\n", 0, TELEGRAM_MESSAGE_LIMIT)
        if split_at < TELEGRAM_MESSAGE_LIMIT // 2:
            split_at = normalized.rfind(" ", 0, TELEGRAM_MESSAGE_LIMIT)
        if split_at < TELEGRAM_MESSAGE_LIMIT // 2:
            split_at = TELEGRAM_MESSAGE_LIMIT
        candidate = normalized[:split_at]
        entity_start = candidate.rfind("&")
        if entity_start >= 0 and ";" not in candidate[entity_start:]:
            split_at = entity_start
            candidate = normalized[:split_at]
        if not candidate:
            split_at = TELEGRAM_MESSAGE_LIMIT
            candidate = normalized[:split_at]
        chunks.append(candidate.strip())
        normalized = normalized[split_at:].strip()
    chunks.append(normalized)
    return chunks


def _parse_iso_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def news_entry_id(entry: dict) -> str:
    """Return a stable, URL-safe identifier for a digest entry.

    The identifier intentionally depends on the canonical source URL and the
    visible entry fields.  It is deterministic (so a shared link keeps working
    after a server restart) without exposing a source path or an account id.
    """

    raw_url = str(entry.get("url") or "").strip()
    try:
        normalized_url = canonicalize_url(raw_url) if raw_url else ""
    except ValueError:
        normalized_url = raw_url
    fingerprint = "\x1f".join(
        " ".join(str(entry.get(field) or "").split()).casefold()
        for field in ("title", "meta", "summary", "why")
    )
    digest = hashlib.sha256(f"{normalized_url}\x1e{fingerprint}".encode("utf-8")).hexdigest()
    return f"n_{digest[:24]}"


def _news_record(entry: dict, view: DigestView | None = None) -> dict:
    """Create the serializable representation used by saved and shared news."""

    return {
        "id": news_entry_id(entry),
        "title": str(entry.get("title") or "Senza titolo"),
        "meta": str(entry.get("meta") or ""),
        "url": str(entry.get("url") or ""),
        "summary": str(entry.get("summary") or ""),
        "why": str(entry.get("why") or ""),
        "digest_title": str(view.title) if view else "",
        "digest_date": str(view.digest_date or "") if view else "",
    }


def _all_news_records() -> list[dict]:
    """Return every distinct news entry available in the digest archive."""

    records: dict[str, dict] = {}
    for item in digest_history():
        view = load_digest_by_date(item.digest_date) if item.digest_date else load_digest_by_id(item.id)
        if view is None:
            continue
        for entry in view.entries:
            record = _news_record(entry, view)
            # The latest digest representation wins when a source appears more
            # than once, while keeping a single stable shared link.
            records[record["id"]] = record
    return sorted(
        records.values(),
        key=lambda record: (record.get("digest_date") or "", record.get("title") or ""),
        reverse=True,
    )


def _history_news_records(history: list[DigestHistoryItem]) -> list[dict]:
    """Return every archived entry while preserving its digest-date grouping."""
    records = []
    for item in history:
        view = load_digest_week_by_date(item.digest_date) if item.digest_date else load_digest_by_id(item.id)
        if view is None:
            continue
        records.extend(_news_record(entry, view) for entry in view.entries)
    return records


def find_news_record(news_id: str) -> dict | None:
    if not re.fullmatch(r"n_[a-f0-9]{24}", str(news_id or "")):
        return None
    return next((record for record in _all_news_records() if record["id"] == news_id), None)


def _saved_news_store() -> dict:
    try:
        payload = json.loads(SAVED_NEWS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"users": {}}
    users = payload.get("users") if isinstance(payload, dict) else None
    if not isinstance(users, dict):
        return {"users": {}}
    return {"users": users}


def _write_saved_news_store(store: dict) -> None:
    SAVED_NEWS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = SAVED_NEWS_PATH.with_suffix(f"{SAVED_NEWS_PATH.suffix}.tmp")
    temporary_path.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary_path.replace(SAVED_NEWS_PATH)


def _user_storage_key(user: dict | str | None) -> str:
    if isinstance(user, dict):
        value = user.get("id") or user.get("email")
    else:
        value = user
    return str(value or "").strip()


def saved_news_for_user(user: dict | str | None) -> list[dict]:
    user_key = _user_storage_key(user)
    if not user_key:
        return []
    with _SAVED_NEWS_LOCK:
        store = _saved_news_store()
        saved = store["users"].get(user_key, [])
        if not isinstance(saved, list):
            return []
        return [dict(record) for record in saved if isinstance(record, dict)]


def saved_news_ids_for_user(user: dict | str | None) -> set[str]:
    return {str(record.get("id") or "") for record in saved_news_for_user(user)}


def saved_news_record_for_user(user: dict | str | None, news_id: str) -> dict | None:
    return next(
        (record for record in saved_news_for_user(user) if str(record.get("id") or "") == news_id),
        None,
    )


def save_news_for_user(user: dict | str | None, record: dict) -> bool:
    """Persist a snapshot of a news item.  Returns True only for a new save."""

    user_key = _user_storage_key(user)
    news_id = str(record.get("id") or "")
    if not user_key or not re.fullmatch(r"n_[a-f0-9]{24}", news_id):
        return False
    with _SAVED_NEWS_LOCK:
        store = _saved_news_store()
        saved = store["users"].setdefault(user_key, [])
        if not isinstance(saved, list):
            saved = []
            store["users"][user_key] = saved
        if any(str(item.get("id") or "") == news_id for item in saved if isinstance(item, dict)):
            return False
        snapshot = {
            key: str(record.get(key) or "")
            for key in ("id", "title", "meta", "url", "summary", "why", "digest_title", "digest_date")
        }
        snapshot["saved_at"] = datetime.now().astimezone().isoformat()
        saved.insert(0, snapshot)
        _write_saved_news_store(store)
    return True


def remove_saved_news_for_user(user: dict | str | None, news_id: str) -> bool:
    user_key = _user_storage_key(user)
    if not user_key:
        return False
    with _SAVED_NEWS_LOCK:
        store = _saved_news_store()
        saved = store["users"].get(user_key, [])
        if not isinstance(saved, list):
            return False
        kept = [item for item in saved if not isinstance(item, dict) or item.get("id") != news_id]
        if len(kept) == len(saved):
            return False
        store["users"][user_key] = kept
        _write_saved_news_store(store)
    return True


def _render_entry(
    entry: dict,
    *,
    user: dict | None = None,
    saved_ids: set[str] | None = None,
    next_path: str = "/",
    news_id: str | None = None,
) -> str:
    record = _news_record(entry)
    candidate_id = str(news_id or "")
    news_id = candidate_id if re.fullmatch(r"n_[a-f0-9]{24}", candidate_id) else record["id"]
    is_saved = news_id in (saved_ids or set())
    url = html.escape(entry.get("url", ""))
    title = html.escape(normalize_news_title(entry.get("title") or "Senza titolo"))
    meta = html.escape(_display_meta_line(entry.get("meta", "")))
    summary = html.escape(entry.get("summary", ""))
    why = html.escape(entry.get("why", ""))
    if user:
        bookmark = f'''<button class="entry-action bookmark-action{' is-saved' if is_saved else ''}" type="button"
    data-news-id="{news_id}" aria-pressed="{'true' if is_saved else 'false'}"
    aria-label="{'Rimuovi dalle notizie salvate' if is_saved else 'Salva notizia'}"
    title="{'Rimuovi dalle notizie salvate' if is_saved else 'Salva notizia'}">&#128278;</button>'''
    else:
        login_href = f"/login?next={quote(next_path, safe='')}"
        bookmark = f'''<a class="entry-action bookmark-action" href="{html.escape(login_href, quote=True)}"
    aria-label="Accedi per salvare la notizia" title="Accedi per salvare la notizia">&#128278;</a>'''
    return f"""<article>
  <span class="label label-placeholder" aria-hidden="true"></span>
  <div class="entry-heading"><h2>{title}</h2>{bookmark}</div>
  <div class="meta">{meta}</div>
  <p>{summary}</p>
  <p><strong>Perché conta:</strong> {why}</p>
  <div class="entry-link-row">
    <a class="link" href="{url}" target="_blank" rel="noreferrer">{url}</a>
    <button class="entry-action share-action" type="button" data-share-path="/share/{news_id}"
      aria-label="Condividi questa notizia" title="Condividi questa notizia">&#128279;</button>
  </div>
</article>"""


def _render_empty_entry() -> str:
    return """<article>
  <span class="label label-placeholder" aria-hidden="true"></span>
  <h2>Nessuna voce disponibile.</h2>
  <div class="meta">Aggiornamento settimanale</div>
  <p>oggi non nuove notizie</p>
  <p><strong>Perché conta:</strong> Il digest resta vuoto perche non sono emerse novita rilevanti nella finestra configurata.</p>
  <span class="link" aria-hidden="true">&nbsp;</span>
</article>"""


def _display_meta_line(meta: str) -> str:
    text = str(meta or "")
    return re.sub(
        r"(Categoria:\s*)([A-Za-z0-9_]+)",
        lambda match: f"{match.group(1)}{_display_category(match.group(2))}",
        text,
    )


def _display_category(value: str) -> str:
    return " ".join(part for part in str(value or "").replace("_", " ").split())


def _render_history_source_list(records: list[dict]) -> str:
    items = []
    for record in records:
        source = html.escape(_news_source_label(record))
        url = str(record.get("url") or "").strip()
        direct_link = (
            f'<a class="history-source-link" href="{html.escape(url, quote=True)}" '
            f'target="_blank" rel="noopener noreferrer">{html.escape(url)}</a>'
            if url
            else ""
        )
        items.append(
            f'''<li class="history-source-card">
  <span class="history-source-title">{source}</span>
  {direct_link}
</li>'''
        )
    if not items:
        return ""
    return f'<ul class="history-source-list">{"".join(items)}</ul>'


def _render_history_item(
    item: DigestHistoryItem,
    *,
    index: int = 0,
    news_records: list[dict] | None = None,
    search_text: str = "",
    search_title: str = "",
    search_source: str = "",
    search_suggestions: list[str] | None = None,
) -> str:
    active_class = " active" if item.active else ""
    open_attr = " open" if item.active else ""
    href = f"/digest?date={quote(item.digest_date, safe='')}" if item.digest_date else f"/digest?id={quote(item.id, safe='')}"
    entry_label = "voce" if item.entry_count == 1 else "voci"
    display_title = _display_digest_title(item.title, item.updated)
    search_text = search_text or " ".join([item.title, item.subtitle, item.updated, *(item.key_points or [])])
    search_title = search_title or display_title
    search_source = search_source or item.subtitle
    history_sources = _render_history_source_list(news_records or [])
    suggestions = html.escape(
        json.dumps(search_suggestions or [], ensure_ascii=False), quote=True
    )
    return f"""<details class="history-item{active_class}"{open_attr} data-history-item
  data-search="{html.escape(search_text, quote=True)}"
  data-title="{html.escape(search_title, quote=True)}"
  data-source="{html.escape(search_source, quote=True)}"
  data-suggestions="{suggestions}" data-index="{index}">
  <summary class="history-summary">
    <span>
      <span class="history-title">{html.escape(display_title)}</span>
      <span class="history-meta">{html.escape(item.updated)} &middot; {item.entry_count} {entry_label}</span>
    </span>
    <span class="history-arrow" aria-hidden="true">&#9662;</span>
  </summary>
  <div class="history-body">
    {history_sources}
    <a class="read-more" href="{href}" target="_blank" rel="noopener noreferrer">Leggi di pi&ugrave;</a>
  </div>
</details>"""


def _mark_active_history(
    history: list[DigestHistoryItem],
    active_path: str | None,
    active_date: str | None = None,
) -> list[DigestHistoryItem]:
    active_id = _digest_id(Path(active_path)) if active_path else None
    return [
        DigestHistoryItem(
            id=item.id,
            path=item.path,
            title=item.title,
            subtitle=item.subtitle,
            updated=item.updated,
            key_points=item.key_points,
            entry_count=item.entry_count,
            active=(item.digest_date == active_date if active_date else item.id == active_id),
            digest_date=item.digest_date,
        )
        for item in history
    ]


def _history_key_points(view: DigestView) -> list[str]:
    points = [_title_key_point(entry.get("title", "")) for entry in view.entries if entry.get("title")]
    points = [point for point in points if point]
    return points or ([view.subtitle] if view.subtitle else [])


def _title_key_point(title: str, limit: int = 72) -> str:
    text = " ".join(str(title).split())
    if len(text) <= limit:
        return text
    shortened = text[: limit - 3].rsplit(" ", 1)[0].strip()
    return f"{shortened or text[: limit - 3].strip()}..."


def _display_digest_title(title: str, fallback_updated: str | None = None) -> str:
    match = re.search(r"\b\d{2}/\d{2}/\d{4}\b", title)
    if match:
        return match.group(0)
    if fallback_updated:
        return fallback_updated.split(" ", 1)[0]
    return title.replace("AI Research Digest", "").strip(" -") or title


def _digest_sort_timestamp(view: DigestView, path: Path) -> float:
    return _timestamp_value(_digest_execution_datetime(view, path))


def _digest_execution_datetime(view: DigestView, path: Path) -> datetime:
    generated_at = _generated_at_from_footer(view.footer)
    if generated_at:
        return generated_at
    from_name = _digest_datetime_from_path(path)
    if from_name:
        return from_name
    return datetime.fromtimestamp(path.stat().st_mtime)


def _generated_at_from_footer(footer: list[str]) -> datetime | None:
    for line in footer:
        match = re.search(r"\bGenerato il\s+(?P<value>.*?)(?:\s+-\s+Fonti|\s*$)", line)
        if not match:
            continue
        value = match.group("value").strip()
        parsed = _parse_datetime(value)
        if parsed:
            return parsed
    return None


def _digest_datetime_from_path(path: Path | None) -> datetime | None:
    if not path:
        return None
    match = re.search(
        r"\bdigest_(?P<date>\d{4}-\d{2}-\d{2})(?:_(?P<time>\d{6}))?\b",
        path.name,
    )
    if not match:
        return None
    parsed_date = _parse_iso_date(match.group("date"))
    if parsed_date is None:
        return None
    raw_time = match.group("time")
    if not raw_time:
        return datetime.combine(parsed_date, datetime.min.time())
    return datetime(
        parsed_date.year,
        parsed_date.month,
        parsed_date.day,
        int(raw_time[:2]),
        int(raw_time[2:4]),
        int(raw_time[4:6]),
    )


def _parse_datetime(value: str) -> datetime | None:
    normalized = str(value or "").strip().replace("Z", "+00:00")
    if not normalized:
        return None
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        parsed_date = _parse_iso_date(normalized)
        if parsed_date:
            return datetime.combine(parsed_date, datetime.min.time())
    return None


def _format_history_updated(value: datetime) -> str:
    local_value = value.astimezone() if value.tzinfo else value
    return local_value.strftime("%d/%m/%Y %H:%M")


def _timestamp_value(value: datetime) -> float:
    return value.timestamp()


def _digest_id(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def _collect_digest_paths(digest_dirs: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for directory in digest_dirs:
        root = Path(directory)
        if root.exists():
            files.extend(root.glob("*.md"))
    return files


def _legacy_digest_path(digest_id: str) -> Path | None:
    path = Path(digest_id)
    if path.suffix != ".md" or not path.exists():
        return None
    resolved = path.resolve()
    for directory in DEFAULT_DIGEST_DIRS:
        root = Path(directory).resolve()
        try:
            resolved.relative_to(root)
            return path
        except ValueError:
            continue
    return None


def _digest_date_key(view: DigestView, path: Path | None = None) -> str | None:
    return view.digest_date or _digest_date_from_path(path) or _digest_date_from_title(view.title)


def _digest_date_from_path(path: Path | None) -> str | None:
    if not path:
        return None
    match = re.search(r"\bdigest_(\d{4}-\d{2}-\d{2})\b", path.name)
    return match.group(1) if match else None


def _digest_date_from_title(title: str) -> str | None:
    match = re.search(r"\b(\d{2})/(\d{2})/(\d{4})\b", title)
    if not match:
        return None
    day, month, year = match.groups()
    return f"{year}-{month}-{day}"


def _merge_digest_views(
    views: list[DigestView],
    source_path: Path | None,
    digest_date: str,
) -> DigestView:
    latest = views[-1]
    entries = _dedupe_view_entries(entry for view in views for entry in view.entries)
    return DigestView(
        path=str(source_path) if source_path else latest.path,
        title=f"AI Research Digest - {_format_date_for_title(digest_date)}",
        subtitle=_subtitle_with_entry_count(latest.subtitle, len(entries)),
        warning=latest.warning,
        entries=entries,
        footer=latest.footer,
        digest_date=digest_date,
    )


def _dedupe_view_entries(entries) -> list[dict]:
    unique: dict[str, dict] = {}
    for entry in entries:
        url = entry.get("url") or ""
        key = canonicalize_url(url) if url else _normalized_history_title(entry.get("title", ""))
        if key and key not in unique:
            unique[key] = entry
    return list(unique.values())


def _normalized_history_title(title: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(title).casefold()))


def _subtitle_with_entry_count(subtitle: str, entry_count: int) -> str:
    base = subtitle or "Ricerca AI / modelli"
    replacement = f"- {entry_count} {'voce' if entry_count == 1 else 'voci'}"
    if re.search(r"-\s*\d+\s+voc[ei]\b", base):
        return re.sub(r"-\s*\d+\s+voc[ei]\b", replacement, base)
    return f"{base} {replacement}"


def _format_date_for_title(digest_date: str) -> str:
    year, month, day = digest_date.split("-")
    return f"{day}/{month}/{year}"


def _empty_digest_view(
    title: str,
    subtitle: str,
    digest_date: str | None = None,
) -> DigestView:
    return DigestView(
        path=None,
        title=title,
        subtitle=subtitle,
        warning=None,
        entries=[],
        footer=[],
        digest_date=digest_date,
    )


def _first_line(lines: list[str], prefix: str, default):
    for line in lines:
        if line.startswith(prefix):
            return line.removeprefix(prefix).strip()
    return default


def _strip_markdown_bold(line: str) -> str:
    return line.replace("**", "").strip()


def _clean_entry(entry: dict) -> dict:
    cleaned = {key: " ".join(str(value).split()) for key, value in entry.items()}
    cleaned["url"] = _extract_url(cleaned.get("url", ""))
    cleaned["summary"] = _strip_markdown_bold(cleaned.get("summary", ""))
    cleaned["why"] = _strip_markdown_bold(cleaned.get("why", ""))
    return cleaned


def _split_markdown_label(line: str) -> tuple[str | None, str]:
    match = re.match(r"^\*\*(?P<label>[^:]+):\*\*\s*(?P<value>.*)$", line.strip())
    if not match:
        return None, line.strip()
    return match.group("label").strip().casefold(), match.group("value").strip()


def _extract_url(value: str) -> str:
    text = value.strip()
    markdown_link = re.match(r"^\[(?P<label>[^\]]+)\]\((?P<url>[^)]+)\)$", text)
    if markdown_link:
        return markdown_link.group("url").strip()
    return text


def _is_footer_line(line: str) -> bool:
    return line.startswith("*Generato") or line.startswith("*Fonti")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Interfaccia grafica locale per i digest")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    run_server(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
