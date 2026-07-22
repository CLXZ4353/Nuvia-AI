"""Firebase Firestore persistence layer for the Research Digest Agent.

This module provides Firestore-backed implementations for all data that the
application currently stores in local JSON files.  Each store implements the
same interface as its local counterpart, so callers can switch between local
and Firestore storage transparently.

Usage:
    stores = create_firestore_store(config)
    if stores:
        auth.set_auth_store(stores["auth"])
        # … inject other stores into the GUI …
"""

from __future__ import annotations

import json
import os
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from google.cloud import firestore as gcp_firestore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIRESTORE_ENABLED = False
_COLLECTION_PREFIX = "research_digest"
_client_instance: gcp_firestore.Client | None = None
_client_lock = threading.RLock()


def _get_client(config: dict | None = None) -> gcp_firestore.Client | None:
    """Return the shared Firestore client, initialising it once."""
    global _client_instance
    if _client_instance is not None:
        return _client_instance
    with _client_lock:
        if _client_instance is not None:
            return _client_instance
        try:
            project_id = os.getenv("FIREBASE_PROJECT_ID") or ""
            credentials_json = os.getenv("FIREBASE_CREDENTIALS_JSON") or ""
            if not project_id or not credentials_json:
                return None
            from google.oauth2 import service_account

            creds = service_account.Credentials.from_service_account_info(
                json.loads(credentials_json)
            )
            _client_instance = gcp_firestore.Client(
                project=project_id,
                credentials=creds,
            )
        except Exception:
            return None
    return _client_instance


def _collection_path(kind: str) -> str:
    """Return the full document-collection path for a store kind."""
    return f"{_COLLECTION_PREFIX}_{kind}"


def _doc_id(value: str) -> str:
    """Normalise a document identifier for Firestore (lowercase, no leading /)."""
    return str(value or "").strip().casefold().replace("/", "_") or "_empty"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# FirestoreAuthStore
# ---------------------------------------------------------------------------


class FirestoreAuthStore:
    """Firestore-backed auth store that mirrors the local JSON file contract.

    The ``_load_store`` / ``_save_store`` pattern from ``src/auth.py`` is
    preserved so the calling code can switch between the two transparently.
    """

    def __init__(self, db: gcp_firestore.Client) -> None:
        self._db = db
        self._users_ref = db.collection(_collection_path("auth_users"))
        self._sessions_ref = db.collection(_collection_path("auth_sessions"))
        self._intents_ref = db.collection(_collection_path("auth_telegram_intents"))

    # -- users ------------------------------------------------------------

    def get_all_users(self) -> list[dict[str, Any]]:
        docs = self._users_ref.stream()
        return [doc.to_dict() for doc in docs]

    def get_user_by_id(self, user_id: str) -> dict[str, Any] | None:
        doc = self._users_ref.document(_doc_id(user_id)).get()
        return doc.to_dict() if doc.exists else None

    def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        normalized = _doc_id(email)
        doc = self._users_ref.document(normalized).get()
        return doc.to_dict() if doc.exists else None

    def save_user(self, user: dict[str, Any]) -> None:
        doc_id = _doc_id(user.get("id", ""))
        self._users_ref.document(doc_id).set(user)

    def update_user(self, user_id: str, updates: dict[str, Any]) -> None:
        self._users_ref.document(_doc_id(user_id)).update(updates)

    def delete_user(self, user_id: str) -> None:
        self._users_ref.document(_doc_id(user_id)).delete()

    # -- sessions ---------------------------------------------------------

    def get_session(self, token_digest: str) -> dict[str, Any] | None:
        doc = self._sessions_ref.document(_doc_id(token_digest)).get()
        return doc.to_dict() if doc.exists else None

    def save_session(self, token_digest: str, session: dict[str, Any]) -> None:
        self._sessions_ref.document(_doc_id(token_digest)).set(session)

    def delete_session(self, token_digest: str) -> None:
        self._sessions_ref.document(_doc_id(token_digest)).delete()

    def list_sessions(self) -> list[dict[str, Any]]:
        docs = self._sessions_ref.stream()
        return [{"key": doc.id, **doc.to_dict()} for doc in docs]

    # -- telegram connect intents -----------------------------------------

    def get_telegram_intents(self) -> list[dict[str, Any]]:
        docs = self._intents_ref.stream()
        return [doc.to_dict() for doc in docs]

    def save_telegram_intent(self, intent: dict[str, Any]) -> None:
        payload_hash = intent.get("payload_hash", "")
        self._intents_ref.document(_doc_id(payload_hash)).set(intent)

    def delete_telegram_intent(self, payload_hash: str) -> None:
        self._intents_ref.document(_doc_id(payload_hash)).delete()

    def set_telegram_intents(self, intents: list[dict[str, Any]]) -> None:
        """Replace all intents (used by pruning)."""
        batch = self._db.batch()
        for intent in intents:
            payload_hash = intent.get("payload_hash", "")
            ref = self._intents_ref.document(_doc_id(payload_hash))
            batch.set(ref, intent)
        batch.commit()


# ---------------------------------------------------------------------------
# FirestoreSubscriberStore
# ---------------------------------------------------------------------------


class FirestoreSubscriberStore:
    """Firestore-backed newsletter subscriber store."""

    def __init__(self, db: gcp_firestore.Client) -> None:
        self._ref = db.collection(_collection_path("newsletter_subscribers"))

    def get_all(self) -> list[str]:
        docs = self._ref.stream()
        return [doc.id for doc in docs]

    def add(self, email: str) -> None:
        self._ref.document(_doc_id(email)).set({"email": email, "created_at": _now_iso()})

    def remove(self, email: str) -> None:
        self._ref.document(_doc_id(email)).delete()

    def exists(self, email: str) -> bool:
        doc = self._ref.document(_doc_id(email)).get()
        return doc.exists


# ---------------------------------------------------------------------------
# FirestoreTelegramStore
# ---------------------------------------------------------------------------


class FirestoreTelegramStore:
    """Firestore-backed store for Telegram subscribers, contacts, and state."""

    def __init__(self, db: gcp_firestore.Client) -> None:
        self._db = db
        self._subscribers_ref = db.collection(_collection_path("telegram_subscribers"))
        self._contacts_ref = db.collection(_collection_path("telegram_contacts"))
        self._start_state_ref = db.collection(_collection_path("telegram_start_state"))
        self._generation_state_ref = db.collection(_collection_path("telegram_generation_state"))
        self._delivery_state_ref = db.collection(_collection_path("telegram_delivery_state"))

    # -- subscribers ------------------------------------------------------

    def get_subscriber_chat_ids(self) -> list[str]:
        docs = self._subscribers_ref.stream()
        return [doc.id for doc in docs]

    def add_subscriber(self, chat_id: str) -> None:
        self._subscribers_ref.document(_doc_id(chat_id)).set(
            {"chat_id": chat_id, "created_at": _now_iso()}
        )

    def remove_subscriber(self, chat_id: str) -> None:
        self._subscribers_ref.document(_doc_id(chat_id)).delete()

    # -- contacts ---------------------------------------------------------

    def get_contact(self, chat_id: str) -> dict[str, Any] | None:
        doc = self._contacts_ref.document(_doc_id(chat_id)).get()
        return doc.to_dict() if doc.exists else None

    def save_contact(self, chat_id: str, contact: dict[str, Any]) -> None:
        self._contacts_ref.document(_doc_id(chat_id)).set(contact)

    def get_all_contacts(self) -> dict[str, dict]:
        docs = self._contacts_ref.stream()
        return {doc.id: doc.to_dict() for doc in docs}

    # -- start state ------------------------------------------------------

    def get_start_state(self) -> dict[str, Any]:
        doc = self._start_state_ref.document("state").get()
        return doc.to_dict() if doc.exists else {}

    def save_start_state(self, state: dict[str, Any]) -> None:
        self._start_state_ref.document("state").set(state)

    # -- generation state -------------------------------------------------

    def get_generation_state(self) -> dict[str, Any]:
        doc = self._generation_state_ref.document("state").get()
        return doc.to_dict() if doc.exists else {}

    def save_generation_state(self, state: dict[str, Any]) -> None:
        self._generation_state_ref.document("state").set(state)

    # -- delivery state ---------------------------------------------------

    def get_delivery_state(self) -> dict[str, Any]:
        doc = self._delivery_state_ref.document("state").get()
        return doc.to_dict() if doc.exists else {}

    def save_delivery_state(self, state: dict[str, Any]) -> None:
        self._delivery_state_ref.document("state").set(state)


# ---------------------------------------------------------------------------
# FirestoreSavedNewsStore
# ---------------------------------------------------------------------------


class FirestoreSavedNewsStore:
    """Firestore-backed store for per-user saved news."""

    def __init__(self, db: gcp_firestore.Client) -> None:
        self._ref = db.collection(_collection_path("saved_news"))

    def get_saved_ids(self, user_id: str) -> set[str]:
        doc = self._ref.document(_doc_id(user_id)).get()
        data = doc.to_dict() if doc.exists else {}
        return set(data.get("news_ids", []))

    def get_saved_records(self, user_id: str) -> list[dict[str, Any]]:
        doc = self._ref.document(_doc_id(user_id)).get()
        data = doc.to_dict() if doc.exists else {}
        return data.get("records", [])

    def save_news_for_user(
        self, user_id: str, news_id: str, record: dict[str, Any]
    ) -> bool:
        doc_ref = self._ref.document(_doc_id(user_id))
        doc = doc_ref.get()
        data = doc.to_dict() if doc.exists else {"news_ids": [], "records": []}
        if news_id in data["news_ids"]:
            return False
        data["news_ids"].append(news_id)
        data["records"].append(record)
        doc_ref.set(data)
        return True

    def remove_news_for_user(self, user_id: str, news_id: str) -> bool:
        doc_ref = self._ref.document(_doc_id(user_id))
        doc = doc_ref.get()
        if not doc.exists:
            return False
        data = doc.to_dict()
        if news_id not in data.get("news_ids", []):
            return False
        indices = [
            i for i, nid in enumerate(data["news_ids"]) if nid == news_id
        ]
        for i in reversed(indices):
            data["news_ids"].pop(i)
            if i < len(data.get("records", [])):
                data["records"].pop(i)
        doc_ref.set(data)
        return True

    def get_record_for_user(self, user_id: str, news_id: str) -> dict | None:
        data = self.get_saved_records(user_id)
        for record in data:
            if record.get("id") == news_id:
                return record
        return None


# ---------------------------------------------------------------------------
# FirestoreNotificationStore
# ---------------------------------------------------------------------------


class FirestoreNotificationStore:
    """Firestore-backed store for notification suspensions (global and per-user)."""

    def __init__(self, db: gcp_firestore.Client) -> None:
        self._global_ref = db.collection(_collection_path("notification_suspension"))
        self._user_ref = db.collection(_collection_path("user_notification_suspensions"))

    # -- global suspension ------------------------------------------------

    def get_global_suspension(self) -> dict[str, Any] | None:
        doc = self._global_ref.document("suspension").get()
        return doc.to_dict() if doc.exists else None

    def save_global_suspension(
        self, start_iso: str, end_iso: str
    ) -> None:
        self._global_ref.document("suspension").set(
            {"start": start_iso, "end": end_iso}
        )

    def clear_global_suspension(self) -> None:
        self._global_ref.document("suspension").delete()

    # -- per-user suspension ----------------------------------------------

    def get_user_suspension(self, user_id: str) -> dict[str, Any] | None:
        doc = self._user_ref.document(_doc_id(user_id)).get()
        return doc.to_dict() if doc.exists else None

    def save_user_suspension(
        self, user_id: str, start_iso: str, end_iso: str
    ) -> None:
        self._user_ref.document(_doc_id(user_id)).set(
            {"start": start_iso, "end": end_iso}
        )

    def clear_user_suspension(self, user_id: str) -> None:
        self._user_ref.document(_doc_id(user_id)).delete()

    def get_all_user_suspensions(self) -> dict[str, dict[str, Any]]:
        docs = self._user_ref.stream()
        return {doc.id: doc.to_dict() for doc in docs}


# ---------------------------------------------------------------------------
# FirestoreFCMTokenStore
# ---------------------------------------------------------------------------


class FirestoreFCMTokenStore:
    """Firestore-backed store for FCM push notification tokens."""

    def __init__(self, db: gcp_firestore.Client) -> None:
        self._ref = db.collection(_collection_path("fcm_tokens"))

    def get_all_tokens(self) -> list[dict[str, Any]]:
        docs = self._ref.stream()
        return [doc.to_dict() for doc in docs]

    def save_token(
        self, token: str, user_id: str | None = None
    ) -> None:
        data: dict[str, Any] = {
            "token": token,
            "created_at": _now_iso(),
        }
        if user_id:
            data["user_id"] = user_id
        self._ref.document(_doc_id(token)).set(data)

    def remove_token(self, token: str, user_id: str | None = None) -> bool:
        doc_ref = self._ref.document(_doc_id(token))
        doc = doc_ref.get()
        if not doc.exists:
            return False
        data = doc.to_dict()
        if user_id and data.get("user_id") not in {None, "", user_id}:
            return False
        doc_ref.delete()
        return True

    def token_registered_for_user(self, token: str, user_id: str) -> bool:
        doc = self._ref.document(_doc_id(token)).get()
        if not doc.exists:
            return False
        data = doc.to_dict()
        return data.get("user_id") == user_id


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_firestore_store(config: dict | None = None) -> dict[str, Any] | None:
    """Create all Firestore stores if Firestore is configured and enabled.

    Args:
        config: Application configuration dict (from ``config.yaml``).
                If ``None``, reads from environment variables.

    Returns:
        A dict with keys ``auth``, ``subscriber``, ``telegram``,
        ``saved_news``, ``notification``, ``fcm_token``, or ``None``
        if Firestore is not enabled or not configured.
    """
    # Determine whether Firestore is enabled
    firestore_cfg = {}
    if config and isinstance(config, dict):
        firestore_cfg = config.get("firestore", {}) or {}
    enabled = firestore_cfg.get("enabled", False)
    if isinstance(enabled, str):
        enabled = enabled.strip().casefold() in {"1", "true", "yes", "on"}
    if not enabled:
        return None

    global _COLLECTION_PREFIX
    _COLLECTION_PREFIX = str(
        firestore_cfg.get("collection_prefix", "research_digest")
    )

    db = _get_client(config)
    if db is None:
        return None

    return {
        "auth": FirestoreAuthStore(db),
        "subscriber": FirestoreSubscriberStore(db),
        "telegram": FirestoreTelegramStore(db),
        "saved_news": FirestoreSavedNewsStore(db),
        "notification": FirestoreNotificationStore(db),
        "fcm_token": FirestoreFCMTokenStore(db),
    }
