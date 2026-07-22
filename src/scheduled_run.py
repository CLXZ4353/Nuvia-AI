"""Timezone-safe entry point for the GitHub Actions digest schedule."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from src.config import load_config
from src.main import main as run_digest


logger = logging.getLogger(__name__)


def scheduled_run_due(config_path: str = "config.yaml", now: datetime | None = None) -> bool:
    """Return whether ``now`` is the configured local publication hour.

    GitHub Actions cron is UTC-only.  The workflow wakes at both possible UTC
    hours and this guard runs the digest only at the configured Europe/Rome
    wall-clock hour, across both CET and CEST.
    """

    config = load_config(config_path)
    schedule = config.get("schedule", {}) if isinstance(config, dict) else {}
    timezone_name = str(schedule.get("timezone") or "UTC")
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        timezone = ZoneInfo("UTC")
    cron = str(schedule.get("cron") or "").split()
    if len(cron) != 5:
        raise ValueError("schedule.cron deve essere un'espressione cron a cinque campi.")
    try:
        minute = int(cron[0])
        hour = int(cron[1])
    except ValueError as exc:
        raise ValueError("schedule.cron deve avere minuto e ora numerici.") from exc
    if not (0 <= minute <= 59 and 0 <= hour <= 23):
        raise ValueError("schedule.cron contiene un orario non valido.")

    current = now or datetime.now(timezone)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone)
    else:
        current = current.astimezone(timezone)
    return current.weekday() < 5 and current.hour == hour and current.minute >= minute


def main() -> int:
    load_dotenv()
    config_path = os.getenv("RESEARCH_DIGEST_CONFIG_PATH", "config.yaml")
    if os.getenv("GITHUB_EVENT_NAME") == "schedule" and not scheduled_run_due(config_path):
        logger.info("Run pianificata ignorata: non è l'orario locale configurato")
        return 0
    return run_digest(["--config", config_path])


if __name__ == "__main__":
    raise SystemExit(main())
