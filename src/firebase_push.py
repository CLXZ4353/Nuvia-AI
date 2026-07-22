from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src import notifications
from src.config import load_config
from src.schemas import Digest


FIREBASE_JS_VERSION = "10.13.2"
DEFAULT_CONFIG_PATH = "config.yaml"
_TOKEN_STORE_LOCK = threading.RLock()


@dataclass(frozen=True)
class FirebasePushSettings:
    enabled: bool
    project_id: str
    credentials_path: Path | None
    token_store_path: Path
    default_topic: str
    web_config: dict[str, str]
    vapid_key: str

    @property
    def web_configured(self) -> bool:
        required = ("apiKey", "projectId", "messagingSenderId", "appId")
        return bool(self.enabled and self.vapid_key and all(self.web_config.get(key) for key in required))

    @property
    def server_configured(self) -> bool:
        return bool(self.enabled and self.project_id and self.credentials_path and self.credentials_path.exists())


@dataclass(frozen=True)
class PushDeliveryResult:
    sent: int = 0
    failed: int = 0
    skipped_reason: str | None = None


def load_firebase_settings(config: dict | None = None, config_path: str = DEFAULT_CONFIG_PATH) -> FirebasePushSettings:
    if config is None:
        try:
            config = load_config(os.getenv("RESEARCH_DIGEST_CONFIG_PATH") or config_path)
        except (FileNotFoundError, ValueError):
            config = {}

    firebase_config = (config.get("notifications", {}) or {}).get("firebase", {}) or {}
    web = firebase_config.get("web", {}) or {}

    project_id = _env_or_config("FIREBASE_PROJECT_ID", firebase_config.get("project_id"))
    credentials_path = _path_from_value(
        _env_or_config("FIREBASE_CREDENTIALS_PATH", firebase_config.get("credentials_path"))
    )
    token_store_path = _path_from_value(
        _env_or_config("FIREBASE_TOKEN_STORE_PATH", firebase_config.get("token_store_path"))
    ) or Path("data/firebase/fcm_tokens.json")

    web_config = {
        "apiKey": _env_or_config("FIREBASE_WEB_API_KEY", web.get("api_key")),
        "authDomain": _env_or_config("FIREBASE_AUTH_DOMAIN", web.get("auth_domain")),
        "projectId": project_id,
        "storageBucket": _env_or_config("FIREBASE_STORAGE_BUCKET", web.get("storage_bucket")),
        "messagingSenderId": _env_or_config("FIREBASE_MESSAGING_SENDER_ID", web.get("messaging_sender_id")),
        "appId": _env_or_config("FIREBASE_APP_ID", web.get("app_id")),
        "measurementId": _env_or_config("FIREBASE_MEASUREMENT_ID", web.get("measurement_id")),
    }

    return FirebasePushSettings(
        enabled=_truthy(_env_or_config("FIREBASE_ENABLED", firebase_config.get("enabled", False))),
        project_id=project_id,
        credentials_path=credentials_path,
        token_store_path=token_store_path,
        default_topic=_env_or_config("FIREBASE_DEFAULT_TOPIC", firebase_config.get("default_topic")) or "research-digest",
        web_config={key: value for key, value in web_config.items() if value},
        vapid_key=_env_or_config("FIREBASE_VAPID_KEY", web.get("vapid_key")),
    )


def public_firebase_config(settings: FirebasePushSettings | None = None) -> dict[str, Any]:
    settings = settings or load_firebase_settings()
    configured = settings.web_configured
    return {
        "enabled": settings.enabled,
        "configured": configured,
        "web": settings.web_config if configured else {},
        "vapidKey": settings.vapid_key if configured else "",
        "defaultTopic": settings.default_topic,
        "sdkVersion": FIREBASE_JS_VERSION,
        "serviceWorkerUrl": "/firebase-messaging-sw.js",
    }


def render_firebase_messaging_service_worker(settings: FirebasePushSettings | None = None) -> str:
    payload = public_firebase_config(settings)
    if not payload["configured"]:
        return """self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
"""

    firebase_config = json.dumps(payload["web"], ensure_ascii=False)
    return f"""importScripts("https://www.gstatic.com/firebasejs/{FIREBASE_JS_VERSION}/firebase-app-compat.js");
importScripts("https://www.gstatic.com/firebasejs/{FIREBASE_JS_VERSION}/firebase-messaging-compat.js");

firebase.initializeApp({firebase_config});
const messaging = firebase.messaging();

messaging.onBackgroundMessage((payload) => {{
  const notification = payload.notification || {{}};
  const data = payload.data || {{}};
  self.registration.showNotification(notification.title || "Research Digest AI", {{
    body: notification.body || "Nuovo digest disponibile.",
    icon: "/assets/images/favicon.svg",
    badge: "/assets/images/favicon.svg",
    data
  }});
}});

self.addEventListener("notificationclick", (event) => {{
  event.notification.close();
  const targetUrl = event.notification.data && event.notification.data.url
    ? event.notification.data.url
    : "/";
  event.waitUntil(self.clients.openWindow(targetUrl));
}});
"""


def save_fcm_token(
    token: str,
    settings: FirebasePushSettings | None = None,
    *,
    user_id: str | None = None,
) -> int:
    """Persist a browser token, optionally associating it with an account.

    Older callers deliberately remain supported: an unowned token can still be
    delivered during a migration, while the web API associates new tokens with
    the authenticated user that registered them.
    """

    settings = settings or load_firebase_settings()
    normalized = _validate_token(token)
    normalized_user_id = _validate_user_id(user_id)
    with _TOKEN_STORE_LOCK:
        token_store = _load_token_store(settings.token_store_path)
        tokens = token_store["tokens"]
        existing = next((item for item in tokens if item.get("token") == normalized), None)
        if existing is None:
            record = {"token": normalized, "created_at": datetime.utcnow().isoformat(timespec="seconds")}
            if normalized_user_id:
                record["user_id"] = normalized_user_id
            tokens.append(record)
        elif normalized_user_id:
            # A browser FCM token is proof of possession of that browser
            # instance. An explicit subscribe action transfers it to the
            # account currently signed in on the shared device.
            existing["user_id"] = normalized_user_id
        _write_token_store(settings.token_store_path, token_store)
        return len(tokens)


def remove_fcm_token(
    token: str,
    settings: FirebasePushSettings | None = None,
    *,
    user_id: str | None = None,
) -> bool:
    """Remove one registered browser token without affecting other accounts."""

    settings = settings or load_firebase_settings()
    normalized = _validate_token(token)
    normalized_user_id = _validate_user_id(user_id)
    with _TOKEN_STORE_LOCK:
        token_store = _load_token_store(settings.token_store_path)
        before = len(token_store["tokens"])
        token_store["tokens"] = [
            item
            for item in token_store["tokens"]
            if not (
                item.get("token") == normalized
                and (not normalized_user_id or item.get("user_id") in {None, "", normalized_user_id})
            )
        ]
        changed = len(token_store["tokens"]) != before
        if changed:
            _write_token_store(settings.token_store_path, token_store)
        return changed


def fcm_token_registered_for_user(
    token: str,
    user_id: str,
    settings: FirebasePushSettings | None = None,
) -> bool:
    """Return whether this browser token belongs to the authenticated account.

    A permission granted by a browser is device-wide, while delivery tokens are
    account-scoped. Keeping these states separate prevents a second user of the
    same browser from seeing another account's registration as their own.
    """

    settings = settings or load_firebase_settings()
    normalized_token = _validate_token(token)
    normalized_user_id = _validate_user_id(user_id)
    if not normalized_user_id:
        return False
    with _TOKEN_STORE_LOCK:
        return any(
            item.get("token") == normalized_token and item.get("user_id") == normalized_user_id
            for item in _load_token_store(settings.token_store_path)["tokens"]
        )


def send_digest_push(digest: Digest, config: dict | None = None) -> PushDeliveryResult:
    if not digest.entries:
        return PushDeliveryResult(skipped_reason="digest senza voci")

    settings = load_firebase_settings(config)
    if not settings.enabled:
        return PushDeliveryResult(skipped_reason="Firebase disattivato")
    if not settings.server_configured:
        return PushDeliveryResult(skipped_reason="credenziali Firebase non configurate")

    project_now = _project_wall_clock(config)
    with _TOKEN_STORE_LOCK:
        tokens = [
            str(item["token"])
            for item in _load_token_store(settings.token_store_path)["tokens"]
            if item.get("token")
            and not notifications.user_notifications_suspended(
                str(item.get("user_id") or ""),
                now=project_now,
            )
        ]
    if not tokens:
        return PushDeliveryResult(skipped_reason="nessun token FCM registrato")

    try:
        import firebase_admin
        from firebase_admin import credentials, messaging
    except ImportError:
        return PushDeliveryResult(skipped_reason="firebase-admin non installato")

    try:
        firebase_admin.get_app()
    except ValueError:
        firebase_admin.initialize_app(
            credentials.Certificate(str(settings.credentials_path)),
            {"projectId": settings.project_id},
        )

    title = "Research Digest AI"
    body = _digest_push_body(digest)
    sent = 0
    failed = 0
    for batch in _chunks(tokens, 500):
        message = messaging.MulticastMessage(
            notification=messaging.Notification(title=title, body=body),
            data={
                "url": "/",
                "digestDate": digest.run_date.isoformat(),
                "entryCount": str(len(digest.entries)),
            },
            tokens=batch,
        )
        response = messaging.send_each_for_multicast(message)
        sent += response.success_count
        failed += response.failure_count
    return PushDeliveryResult(sent=sent, failed=failed)


def _load_token_store(path: Path) -> dict[str, list[dict[str, str]]]:
    if not path.exists():
        return {"tokens": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"tokens": []}
    if isinstance(payload, list):
        return {"tokens": [{"token": str(token)} for token in payload if token]}
    tokens = payload.get("tokens") if isinstance(payload, dict) else []
    if not isinstance(tokens, list):
        return {"tokens": []}
    return {"tokens": [item for item in tokens if isinstance(item, dict) and item.get("token")]}


def _write_token_store(path: Path, payload: dict[str, list[dict[str, str]]]) -> None:
    """Persist a token-store update atomically under the process lock."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            temporary_path = Path(handle.name)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink(missing_ok=True)


def _validate_token(token: str) -> str:
    normalized = str(token or "").strip()
    if len(normalized) < 20 or len(normalized) > 4096 or any(char.isspace() for char in normalized):
        raise ValueError("Token FCM non valido")
    return normalized


def _validate_user_id(value: str | None) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized and not re.fullmatch(r"[a-f0-9]{32}", normalized):
        raise ValueError("Identificativo utente non valido")
    return normalized


def _project_wall_clock(config: dict | None = None) -> datetime:
    """Match account pause checks to the configured project time zone."""

    effective_config = config
    if effective_config is None:
        try:
            effective_config = load_config(DEFAULT_CONFIG_PATH)
        except (FileNotFoundError, ValueError):
            effective_config = {}
    effective_config = effective_config if isinstance(effective_config, dict) else {}
    web = effective_config.get("web") if isinstance(effective_config.get("web"), dict) else {}
    schedule = effective_config.get("schedule") if isinstance(effective_config.get("schedule"), dict) else {}
    timezone_name = str(web.get("timezone") or schedule.get("timezone") or "UTC").strip() or "UTC"
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        timezone = ZoneInfo("UTC")
    return datetime.now(timezone).replace(tzinfo=None)


def _digest_push_body(digest: Digest) -> str:
    first_title = digest.entries[0].title if digest.entries else "Nuovo digest disponibile"
    if len(first_title) > 90:
        first_title = f"{first_title[:87].rstrip()}..."
    entry_label = "notizia" if len(digest.entries) == 1 else "notizie"
    return f"{len(digest.entries)} {entry_label}: {first_title}"


def _env_or_config(name: str, value) -> str:
    env_value = os.getenv(name)
    if env_value is not None and env_value != "":
        return env_value.strip()
    if value is None:
        return ""
    return str(value).strip()


def _path_from_value(value: str) -> Path | None:
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else Path.cwd() / path


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _chunks(values: list[str], size: int):
    for index in range(0, len(values), size):
        yield values[index : index + size]
