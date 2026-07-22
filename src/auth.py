"""Local account and session persistence for the digest web interface.

The GUI is intentionally dependency-free, so this module uses a small JSON store
instead of introducing a database service.  Passwords are never persisted in
plain text and session tokens are only stored as SHA-256 digests.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


AUTH_STORE_PATH = Path("data/auth_store.json")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
USER_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
TELEGRAM_CHAT_ID_PATTERN = re.compile(r"^-?\d+$")
TELEGRAM_CONNECT_PAYLOAD_PATTERN = re.compile(r"^connect_([A-Za-z0-9_-]{16,56})$")
TELEGRAM_CONNECT_INTENT_TTL = timedelta(minutes=15)
TELEGRAM_CONNECT_INTENT_RETENTION = timedelta(days=1)
PASSWORD_ITERATIONS = 310_000
PERSISTENT_SESSION_DAYS = 30
SESSION_HOURS = 12
_STORE_LOCK = threading.RLock()

# Optional Firestore-backed auth store injection point (set by gui.py in
# production deployments).  When not None every read/write goes through the
# remote store instead of the local JSON file.
_auth_store: object | None = None


def set_auth_store(store: object) -> None:
    """Inject a Firestore-backed (or other remote) auth store.

    The store must implement the same internal ``_load_store`` / ``_save_store``
    contract that ``src/auth.py`` uses, i.e. provide ``get_all_users()``,
    ``get_user_by_id()``, ``get_user_by_email()``, ``save_user()``,
    ``update_user()``, ``delete_user()``, ``get_session()``,
    ``save_session()``, ``delete_session()``, ``list_sessions()``,
    ``get_telegram_intents()``, ``save_telegram_intent()``,
    ``delete_telegram_intent()``, and ``set_telegram_intents()``.

    Pass ``None`` to restore local JSON file behaviour.
    """
    global _auth_store
    _auth_store = store


@dataclass(frozen=True)
class AuthResult:
    """Result returned by registration, login and profile mutations."""

    ok: bool
    message: str = ""
    user: dict[str, Any] | None = None


def normalize_email(value: str) -> str:
    return str(value or "").strip().casefold()


def validate_registration(
    first_name: str,
    last_name: str,
    email: str,
    password: str,
    password_confirmation: str,
) -> dict[str, str]:
    """Return field-level validation errors for a registration request."""

    errors: dict[str, str] = {}
    if not _valid_name(first_name):
        errors["first_name"] = "Inserisci un nome valido."
    if not _valid_name(last_name):
        errors["last_name"] = "Inserisci un cognome valido."
    if not EMAIL_PATTERN.fullmatch(normalize_email(email)):
        errors["email"] = "Inserisci un indirizzo email valido."
    password_error = password_validation_error(password)
    if password_error:
        errors["password"] = password_error
    if password != password_confirmation:
        errors["password_confirmation"] = "Le password non coincidono."
    return errors


def password_validation_error(password: str) -> str | None:
    value = str(password or "")
    if len(value) < 8:
        return "La password deve contenere almeno 8 caratteri."
    if not re.search(r"[a-z]", value):
        return "La password deve contenere almeno una lettera minuscola."
    if not re.search(r"[A-Z]", value):
        return "La password deve contenere almeno una lettera maiuscola."
    if not re.search(r"\d", value):
        return "La password deve contenere almeno un numero."
    if not re.search(r"[^\w\s]", value):
        return "La password deve contenere almeno un carattere speciale."
    return None


def register_user(
    first_name: str,
    last_name: str,
    email: str,
    password: str,
    password_confirmation: str,
    *,
    email_notifications: bool = True,
    telegram_notifications: bool = False,
) -> AuthResult:
    errors = validate_registration(first_name, last_name, email, password, password_confirmation)
    if errors:
        return AuthResult(False, next(iter(errors.values())))

    normalized_email = normalize_email(email)
    with _STORE_LOCK:
        store = _load_store()
        if any(user.get("email") == normalized_email for user in store["users"]):
            return AuthResult(False, "Esiste già un account associato a questa email.")
        user = {
            "id": uuid.uuid4().hex,
            "first_name": str(first_name).strip(),
            "last_name": str(last_name).strip(),
            "email": normalized_email,
            "password_hash": _password_hash(password),
            "created_at": _utc_now(),
            "preferences": {
                "email": bool(email_notifications),
                "telegram": bool(telegram_notifications),
            },
        }
        store["users"].append(user)
        _save_store(store)
    return AuthResult(True, "Account creato.", _public_user(user))


def authenticate_user(email: str, password: str) -> AuthResult:
    normalized_email = normalize_email(email)
    with _STORE_LOCK:
        store = _load_store()
        user = next((item for item in store["users"] if item.get("email") == normalized_email), None)
        if not user or not _verify_password(password, str(user.get("password_hash") or "")):
            return AuthResult(False, "Email o password non corretti.")
        return AuthResult(True, "Accesso effettuato.", _public_user(user))


def create_session(user_id: str, *, persistent: bool = False) -> tuple[str, datetime]:
    """Create a session and return the opaque cookie token plus its expiry."""

    duration = timedelta(days=PERSISTENT_SESSION_DAYS) if persistent else timedelta(hours=SESSION_HOURS)
    expires_at = _utc_datetime() + duration
    token = secrets.token_urlsafe(32)
    with _STORE_LOCK:
        store = _load_store()
        if not any(user.get("id") == user_id for user in store["users"]):
            raise ValueError("Utente non trovato.")
        _prune_sessions(store)
        store["sessions"][_token_digest(token)] = {
            "user_id": user_id,
            "expires_at": _datetime_to_string(expires_at),
            "persistent": bool(persistent),
        }
        _save_store(store)
    return token, expires_at


def get_user_for_session(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    with _STORE_LOCK:
        store = _load_store()
        changed = _prune_sessions(store)
        session = store["sessions"].get(_token_digest(token))
        if session is None:
            if changed:
                _save_store(store)
            return None
        user = next((item for item in store["users"] if item.get("id") == session.get("user_id")), None)
        if user is None:
            store["sessions"].pop(_token_digest(token), None)
            _save_store(store)
            return None
        if changed:
            _save_store(store)
        return _public_user(user)


def revoke_session(token: str | None) -> None:
    if not token:
        return
    with _STORE_LOCK:
        store = _load_store()
        if store["sessions"].pop(_token_digest(token), None) is not None:
            _save_store(store)


def update_user_email(user_id: str, email: str) -> AuthResult:
    normalized_email = normalize_email(email)
    if not EMAIL_PATTERN.fullmatch(normalized_email):
        return AuthResult(False, "Inserisci un indirizzo email valido.")
    with _STORE_LOCK:
        store = _load_store()
        user = next((item for item in store["users"] if item.get("id") == user_id), None)
        if user is None:
            return AuthResult(False, "Sessione non valida. Accedi di nuovo.")
        existing = next(
            (item for item in store["users"] if item.get("email") == normalized_email and item.get("id") != user_id),
            None,
        )
        if existing:
            return AuthResult(False, "Questa email è già associata a un altro account.")
        user["email"] = normalized_email
        _save_store(store)
        return AuthResult(True, "Email aggiornata.", _public_user(user))


def update_notification_preferences(
    user_id: str,
    *,
    email_notifications: bool,
    telegram_notifications: bool,
) -> AuthResult:
    with _STORE_LOCK:
        store = _load_store()
        user = next((item for item in store["users"] if item.get("id") == user_id), None)
        if user is None:
            return AuthResult(False, "Sessione non valida. Accedi di nuovo.")
        user["preferences"] = {
            "email": bool(email_notifications),
            "telegram": bool(telegram_notifications),
        }
        _save_store(store)
        return AuthResult(True, "Preferenze notifiche aggiornate.", _public_user(user))


def user_id_for_email(email: str) -> str | None:
    """Return an account ID for a subscribed email, when it exists.

    The delivery layer uses this internal lookup only to honour an
    account-scoped notification suspension; no account data is exposed.
    """

    normalized_email = normalize_email(email)
    if not normalized_email:
        return None
    with _STORE_LOCK:
        store = _load_store()
        user = next((item for item in store["users"] if item.get("email") == normalized_email), None)
        user_id = str(user.get("id") or "") if user else ""
        return user_id if USER_ID_PATTERN.fullmatch(user_id) else None


def telegram_connect_payload(user_id: str) -> str | None:
    """Create a short-lived, single-use Telegram onboarding payload.

    Telegram deep links are visible in the browser and client history, so the
    account identifier is never exposed in them.  Only a hash of the opaque
    payload is persisted server-side.
    """

    normalized_id = str(user_id or "").strip().casefold()
    if not USER_ID_PATTERN.fullmatch(normalized_id):
        return None
    payload = f"connect_{secrets.token_urlsafe(24).rstrip('=')}"
    now = _utc_datetime()
    expires_at = now + TELEGRAM_CONNECT_INTENT_TTL
    with _STORE_LOCK:
        store = _load_store()
        changed = _prune_telegram_connect_intents(store, now)
        if not any(user.get("id") == normalized_id for user in store["users"]):
            if changed:
                _save_store(store)
            return None
        store["telegram_connect_intents"].append(
            {
                "payload_hash": _telegram_payload_hash(payload),
                "user_id": normalized_id,
                "created_at": _datetime_to_string(now),
                "expires_at": _datetime_to_string(expires_at),
            }
        )
        _save_store(store)
    return payload


def user_id_from_telegram_connect_payload(payload: str) -> str | None:
    """Resolve a still-valid onboarding intent without consuming it."""

    normalized_payload = str(payload or "").strip()
    if not TELEGRAM_CONNECT_PAYLOAD_PATTERN.fullmatch(normalized_payload):
        return None
    with _STORE_LOCK:
        store = _load_store()
        changed = _prune_telegram_connect_intents(store)
        intent = _active_telegram_connect_intent(store, normalized_payload)
        user_id = str(intent.get("user_id") or "") if intent else ""
        user_exists = any(user.get("id") == user_id for user in store["users"])
        if changed:
            _save_store(store)
        return user_id if user_exists else None


def connect_telegram_chat_from_payload(
    payload: str,
    chat_id: str,
    *,
    telegram_user_id: str | None = None,
    telegram_username: str | None = None,
    telegram_first_name: str | None = None,
    telegram_last_name: str | None = None,
) -> AuthResult:
    """Atomically consume an onboarding intent and bind its private chat."""

    normalized_payload = str(payload or "").strip()
    normalized_chat_id = str(chat_id or "").strip()
    normalized_telegram_user_id = str(telegram_user_id or normalized_chat_id).strip()
    if (
        not TELEGRAM_CONNECT_PAYLOAD_PATTERN.fullmatch(normalized_payload)
        or not TELEGRAM_CHAT_ID_PATTERN.fullmatch(normalized_chat_id)
        or not TELEGRAM_CHAT_ID_PATTERN.fullmatch(normalized_telegram_user_id)
        or normalized_telegram_user_id != normalized_chat_id
    ):
        return AuthResult(False, "Collegamento Telegram non valido.")

    with _STORE_LOCK:
        store = _load_store()
        _prune_telegram_connect_intents(store)
        intent = _active_telegram_connect_intent(store, normalized_payload)
        if intent is None:
            _save_store(store)
            return AuthResult(False, "Il link Telegram non è valido, è scaduto o è già stato usato.")
        user_id = str(intent.get("user_id") or "")
        user = next((item for item in store["users"] if item.get("id") == user_id), None)
        if user is None:
            intent["consumed_at"] = _utc_now()
            _save_store(store)
            return AuthResult(False, "Il collegamento Telegram non è più valido.")
        owner = next(
            (
                item
                for item in store["users"]
                if str(item.get("telegram_chat_id") or "") == normalized_chat_id
                and item.get("id") != user_id
            ),
            None,
        )
        if owner is not None:
            _save_store(store)
            return AuthResult(False, "Questa chat Telegram è già collegata a un altro account.")

        already_connected = str(user.get("telegram_chat_id") or "") == normalized_chat_id
        user["telegram_chat_id"] = normalized_chat_id
        user["telegram_user_id"] = normalized_telegram_user_id
        user["telegram_username"] = _clean_telegram_profile_value(telegram_username, 64)
        user["telegram_first_name"] = _clean_telegram_profile_value(telegram_first_name, 128)
        user["telegram_last_name"] = _clean_telegram_profile_value(telegram_last_name, 128)
        user["telegram_connected_at"] = _utc_now()
        user["telegram_start_payload_hash"] = _telegram_payload_hash(normalized_payload)
        intent["consumed_at"] = _utc_now()
        intent["used_chat_id"] = normalized_chat_id
        _save_store(store)
        message = "Chat Telegram già collegata." if already_connected else "Chat Telegram collegata."
        return AuthResult(True, message, _public_user(user))


def connect_telegram_chat(user_id: str, chat_id: str) -> AuthResult:
    """Associate one Telegram chat with an account without exposing the chat publicly."""

    normalized_user_id = str(user_id or "").strip().casefold()
    normalized_chat_id = str(chat_id or "").strip()
    if not USER_ID_PATTERN.fullmatch(normalized_user_id) or not TELEGRAM_CHAT_ID_PATTERN.fullmatch(normalized_chat_id):
        return AuthResult(False, "Collegamento Telegram non valido.")
    with _STORE_LOCK:
        store = _load_store()
        user = next((item for item in store["users"] if item.get("id") == normalized_user_id), None)
        if user is None:
            return AuthResult(False, "Il collegamento Telegram non è più valido.")
        owner = next(
            (
                item
                for item in store["users"]
                if str(item.get("telegram_chat_id") or "") == normalized_chat_id
                and item.get("id") != normalized_user_id
            ),
            None,
        )
        if owner is not None:
            return AuthResult(False, "Questa chat Telegram è già collegata a un altro account.")
        already_connected = str(user.get("telegram_chat_id") or "") == normalized_chat_id
        user["telegram_chat_id"] = normalized_chat_id
        user["telegram_connected_at"] = _utc_now()
        _save_store(store)
        message = "Chat Telegram già collegata." if already_connected else "Chat Telegram collegata."
        return AuthResult(True, message, _public_user(user))


def telegram_chat_id_for_user(user_id: str) -> str | None:
    normalized_user_id = str(user_id or "").strip().casefold()
    if not USER_ID_PATTERN.fullmatch(normalized_user_id):
        return None
    with _STORE_LOCK:
        store = _load_store()
        user = next((item for item in store["users"] if item.get("id") == normalized_user_id), None)
        chat_id = str(user.get("telegram_chat_id") or "").strip() if user else ""
        return chat_id if TELEGRAM_CHAT_ID_PATTERN.fullmatch(chat_id) else None


def telegram_chat_owner_user_id(chat_id: str) -> str | None:
    """Return the account currently bound to a Telegram chat, if any."""

    normalized_chat_id = str(chat_id or "").strip()
    if not TELEGRAM_CHAT_ID_PATTERN.fullmatch(normalized_chat_id):
        return None
    with _STORE_LOCK:
        store = _load_store()
        owner = next(
            (user for user in store["users"] if str(user.get("telegram_chat_id") or "") == normalized_chat_id),
            None,
        )
        return str(owner.get("id") or "") if owner else None


def _valid_name(value: str) -> bool:
    normalized = " ".join(str(value or "").split())
    return 1 <= len(normalized) <= 100


def _password_hash(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PASSWORD_ITERATIONS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def _verify_password(password: str, stored_value: str) -> bool:
    try:
        algorithm, iterations, salt_value, digest_value = stored_value.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_value.encode("ascii"), validate=True)
        expected = base64.b64decode(digest_value.encode("ascii"), validate=True)
        actual = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), salt, int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _public_user(user: dict[str, Any]) -> dict[str, Any]:
    preferences = user.get("preferences") or {}
    return {
        "id": str(user.get("id") or ""),
        "first_name": str(user.get("first_name") or ""),
        "last_name": str(user.get("last_name") or ""),
        "email": str(user.get("email") or ""),
        "preferences": {
            "email": bool(preferences.get("email", True)),
            "telegram": bool(preferences.get("telegram", False)),
        },
        "telegram_connected": bool(TELEGRAM_CHAT_ID_PATTERN.fullmatch(str(user.get("telegram_chat_id") or ""))),
    }


def _default_store() -> dict[str, Any]:
    return {"users": [], "sessions": {}, "telegram_connect_intents": []}


def _load_store() -> dict[str, Any]:
    global _auth_store
    if _auth_store is not None:
        try:
            users = _auth_store.get_all_users()
            sessions_list = _auth_store.list_sessions()
            sessions = {item["key"]: {k: v for k, v in item.items() if k != "key"} for item in sessions_list}
            intents = _auth_store.get_telegram_intents()
            return {
                "users": users if isinstance(users, list) else [],
                "sessions": sessions if isinstance(sessions, dict) else {},
                "telegram_connect_intents": intents if isinstance(intents, list) else [],
            }
        except Exception:
            return _default_store()
    try:
        payload = json.loads(AUTH_STORE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _default_store()
    if not isinstance(payload, dict):
        return _default_store()
    users = payload.get("users")
    sessions = payload.get("sessions")
    intents = payload.get("telegram_connect_intents")
    return {
        "users": users if isinstance(users, list) else [],
        "sessions": sessions if isinstance(sessions, dict) else {},
        "telegram_connect_intents": intents if isinstance(intents, list) else [],
    }


def _save_store(store: dict[str, Any]) -> None:
    global _auth_store
    if _auth_store is not None:
        try:
            users = store.get("users", [])
            for user in users:
                user_id = str(user.get("id", ""))
                existing = _auth_store.get_user_by_id(user_id)
                if existing:
                    _auth_store.update_user(user_id, user)
                else:
                    _auth_store.save_user(user)
            sessions = store.get("sessions", {})
            for token_digest, session in sessions.items():
                _auth_store.save_session(token_digest, session)
            intents = store.get("telegram_connect_intents", [])
            _auth_store.set_telegram_intents(intents)
        except Exception:
            pass
        return
    AUTH_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = AUTH_STORE_PATH.with_suffix(f"{AUTH_STORE_PATH.suffix}.tmp")
    temporary_path.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary_path.replace(AUTH_STORE_PATH)


def _prune_sessions(store: dict[str, Any]) -> bool:
    changed = False
    now = _utc_datetime()
    for key, session in list(store["sessions"].items()):
        expires_at = _string_to_datetime(str(session.get("expires_at") or ""))
        if expires_at is None or expires_at <= now:
            store["sessions"].pop(key, None)
            changed = True
    return changed


def _telegram_payload_hash(payload: str) -> str:
    return hashlib.sha256(str(payload or "").encode("utf-8")).hexdigest()


def _active_telegram_connect_intent(store: dict[str, Any], payload: str) -> dict[str, Any] | None:
    payload_hash = _telegram_payload_hash(payload)
    for intent in store.get("telegram_connect_intents", []):
        if not isinstance(intent, dict) or intent.get("consumed_at"):
            continue
        if hmac.compare_digest(str(intent.get("payload_hash") or ""), payload_hash):
            return intent
    return None


def _prune_telegram_connect_intents(store: dict[str, Any], now: datetime | None = None) -> bool:
    now = now or _utc_datetime()
    retained: list[dict[str, Any]] = []
    changed = False
    for intent in store.get("telegram_connect_intents", []):
        if not isinstance(intent, dict):
            changed = True
            continue
        expires_at = _string_to_datetime(str(intent.get("expires_at") or ""))
        consumed_at = _string_to_datetime(str(intent.get("consumed_at") or ""))
        if expires_at is None:
            changed = True
            continue
        if consumed_at is not None and consumed_at + TELEGRAM_CONNECT_INTENT_RETENTION <= now:
            changed = True
            continue
        if consumed_at is None and expires_at <= now:
            changed = True
            continue
        retained.append(intent)
    if len(retained) != len(store.get("telegram_connect_intents", [])):
        changed = True
    store["telegram_connect_intents"] = retained
    return changed


def _clean_telegram_profile_value(value: str | None, maximum_length: int) -> str:
    return " ".join(str(value or "").split())[:maximum_length]


def _utc_datetime() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now() -> str:
    return _datetime_to_string(_utc_datetime())


def _datetime_to_string(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _string_to_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
