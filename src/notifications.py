from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path


NOTIFICATION_SUSPENSION_PATH = Path("data/notification_suspension.json")
USER_NOTIFICATION_SUSPENSIONS_PATH = Path("data/user_notification_suspensions.json")
_USER_SUSPENSIONS_LOCK = threading.RLock()
_USER_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")


@dataclass(frozen=True)
class NotificationSuspension:
    start: datetime | None = None
    end: datetime | None = None

    @property
    def configured(self) -> bool:
        return self.start is not None and self.end is not None

    def is_active(self, now: datetime | None = None) -> bool:
        if not self.configured:
            return False
        current = now or datetime.now()
        return self.start <= current <= self.end


def load_notification_suspension() -> NotificationSuspension:
    if not NOTIFICATION_SUSPENSION_PATH.exists():
        return NotificationSuspension()
    try:
        payload = json.loads(NOTIFICATION_SUSPENSION_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return NotificationSuspension()

    start = _parse_datetime(payload.get("start"))
    end = _parse_datetime(payload.get("end"))
    if start is None or end is None or end < start:
        return NotificationSuspension()
    return NotificationSuspension(start=start, end=end)


def save_notification_suspension(start_date: date, end_date: date) -> NotificationSuspension:
    start = datetime.combine(start_date, time.min)
    end = datetime.combine(end_date, time(23, 59, 59))
    suspension = NotificationSuspension(start=start, end=end)
    NOTIFICATION_SUSPENSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    NOTIFICATION_SUSPENSION_PATH.write_text(
        json.dumps(
            {
                "start": start.isoformat(timespec="seconds"),
                "end": end.isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return suspension


def clear_notification_suspension() -> None:
    try:
        NOTIFICATION_SUSPENSION_PATH.unlink()
    except FileNotFoundError:
        return


def notifications_suspended(now: datetime | None = None) -> bool:
    return load_notification_suspension().is_active(now)


def notification_suspension_status(now: datetime | None = None) -> dict:
    current = now or datetime.now()
    suspension = load_notification_suspension()
    active = suspension.is_active(current)
    remaining = ""
    if active and suspension.end:
        delta = suspension.end - current
        days = delta.days
        hours = delta.seconds // 3600
        minutes = (delta.seconds % 3600) // 60
        if days > 0:
            remaining = f"{days} giorni e {hours} ore"
        elif hours > 0:
            remaining = f"{hours} ore e {minutes} minuti"
        else:
            remaining = f"{max(minutes, 0)} minuti"
    return {
        "configured": suspension.configured,
        "active": active,
        "start": suspension.start,
        "end": suspension.end,
        "remaining": remaining,
    }


def load_user_notification_suspension(user_id: str) -> NotificationSuspension:
    """Return a user-owned notification pause without exposing another account's data.

    The legacy global pause remains supported for existing server-side workflows.
    New web clients use this account-scoped store so a user's settings never mute
    notifications for every other account.
    """

    key = _user_storage_key(user_id)
    if not key:
        return NotificationSuspension()
    with _USER_SUSPENSIONS_LOCK:
        payload = _load_user_suspensions()
        item = payload["users"].get(key)
        return _suspension_from_payload(item)


def save_user_notification_suspension(
    user_id: str,
    start_date: date,
    end_date: date,
) -> NotificationSuspension:
    key = _user_storage_key(user_id)
    if not key:
        raise ValueError("Utente non valido.")
    suspension = NotificationSuspension(
        start=datetime.combine(start_date, time.min),
        end=datetime.combine(end_date, time(23, 59, 59)),
    )
    with _USER_SUSPENSIONS_LOCK:
        payload = _load_user_suspensions()
        payload["users"][key] = {
            "start": suspension.start.isoformat(timespec="seconds"),
            "end": suspension.end.isoformat(timespec="seconds"),
        }
        _write_user_suspensions(payload)
    return suspension


def clear_user_notification_suspension(user_id: str) -> bool:
    key = _user_storage_key(user_id)
    if not key:
        return False
    with _USER_SUSPENSIONS_LOCK:
        payload = _load_user_suspensions()
        if key not in payload["users"]:
            return False
        payload["users"].pop(key, None)
        _write_user_suspensions(payload)
    return True


def user_notifications_suspended(user_id: str, now: datetime | None = None) -> bool:
    return load_user_notification_suspension(user_id).is_active(now)


def user_notification_suspension_status(user_id: str, now: datetime | None = None) -> dict:
    """Return account-scoped pause state and prune an interval that has ended.

    An elapsed end date automatically re-enables notifications.  Removing the
    completed interval also keeps the settings UI from presenting a stale pause
    as a future schedule.
    """

    current = now or datetime.now()
    suspension = load_user_notification_suspension(user_id)
    if suspension.configured and suspension.end and suspension.end < current:
        clear_user_notification_suspension(user_id)
        suspension = NotificationSuspension()
    active = suspension.is_active(current)
    remaining = ""
    if active and suspension.end:
        delta = suspension.end - current
        days = delta.days
        hours = delta.seconds // 3600
        minutes = (delta.seconds % 3600) // 60
        if days > 0:
            remaining = f"{days} giorni e {hours} ore"
        elif hours > 0:
            remaining = f"{hours} ore e {minutes} minuti"
        else:
            remaining = f"{max(minutes, 0)} minuti"
    return {
        "configured": suspension.configured,
        "active": active,
        "start": suspension.start,
        "end": suspension.end,
        "remaining": remaining,
    }


def _parse_datetime(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        return None


def _user_storage_key(user_id: str) -> str:
    value = str(user_id or "").strip().casefold()
    return value if _USER_ID_PATTERN.fullmatch(value) else ""


def _load_user_suspensions() -> dict:
    try:
        payload = json.loads(USER_NOTIFICATION_SUSPENSIONS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"users": {}}
    users = payload.get("users") if isinstance(payload, dict) else None
    return {"users": users if isinstance(users, dict) else {}}


def _write_user_suspensions(payload: dict) -> None:
    USER_NOTIFICATION_SUSPENSIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = USER_NOTIFICATION_SUSPENSIONS_PATH.with_suffix(
        f"{USER_NOTIFICATION_SUSPENSIONS_PATH.suffix}.tmp"
    )
    temporary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary_path.replace(USER_NOTIFICATION_SUSPENSIONS_PATH)


def _suspension_from_payload(value) -> NotificationSuspension:
    item = value if isinstance(value, dict) else {}
    start = _parse_datetime(item.get("start"))
    end = _parse_datetime(item.get("end"))
    if start is None or end is None or end < start:
        return NotificationSuspension()
    return NotificationSuspension(start=start, end=end)
