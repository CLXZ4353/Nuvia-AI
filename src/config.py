from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

DEFAULT_PROJECT_TIMEZONE = "Europe/Rome"


def project_timezone(config: dict | None = None) -> ZoneInfo:
    """Return the project's configured IANA time zone (Europe/Rome by default).

    Every date/time computation that decides "which week these news belong
    to" - digest generation, the scheduled run, the homepage period label and
    the Cronologia grouping - must agree on the same wall clock. Reading this
    from config keeps that single source of truth, and it automatically
    accounts for the CET/CEST daylight-saving switch because ZoneInfo (not a
    fixed UTC offset) is used.
    """

    schedule = (config or {}).get("schedule", {}) if isinstance(config, dict) else {}
    if not isinstance(schedule, dict):
        schedule = {}
    name = str(schedule.get("timezone") or DEFAULT_PROJECT_TIMEZONE).strip() or DEFAULT_PROJECT_TIMEZONE
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo(DEFAULT_PROJECT_TIMEZONE)


def project_now(config: dict | None = None) -> datetime:
    """Current wall-clock datetime in the project time zone (tz-aware)."""

    return datetime.now(project_timezone(config))


def project_today(config: dict | None = None) -> date:
    """Current calendar date in the project time zone."""

    return project_now(config).date()


def load_config(config_path: str = "config.yaml") -> dict:
    config_file = Path(config_path)
    config = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
    if not config.get("beat"):
        raise ValueError("Config mancante: beat")
    if not (config.get("sources") or config.get("fonti") or config.get("source_catalog")):
        raise ValueError("Config mancante: fonti, sources o source_catalog")

    for section_name, key in (("delivery", "primary"),):
        section = config.get(section_name, {})
        primary = section.get(key, {})
        if primary.get("path"):
            primary["path"] = _resolve_path(primary["path"], config_file)
        if section.get("failed_path"):
            section["failed_path"] = _resolve_path(section["failed_path"], config_file)
    if config.get("state", {}).get("db_path"):
        config["state"]["db_path"] = _resolve_path(config["state"]["db_path"], config_file)
    if config.get("source_catalog", {}).get("path"):
        config["source_catalog"]["path"] = _resolve_path(config["source_catalog"]["path"], config_file)
    if config.get("demo", {}).get("feed_path"):
        config["demo"]["feed_path"] = _resolve_path(config["demo"]["feed_path"], config_file)
    if config.get("demo", {}).get("out_dir"):
        config["demo"]["out_dir"] = _resolve_path(config["demo"]["out_dir"], config_file)
    firebase_config = config.get("notifications", {}).get("firebase", {})
    if firebase_config.get("credentials_path"):
        firebase_config["credentials_path"] = _resolve_path(firebase_config["credentials_path"], config_file)
    if firebase_config.get("token_store_path"):
        firebase_config["token_store_path"] = _resolve_path(firebase_config["token_store_path"], config_file)
    return config


def _resolve_path(value: str, config_file: Path) -> str:
    path = Path(value)
    if path.is_absolute():
        return str(path)
    return str(config_file.resolve().parent / path)
