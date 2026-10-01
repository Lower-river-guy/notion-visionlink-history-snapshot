"""Runtime configuration. The Notion token is never logged."""

from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

VERSION = "0.01.01"

SOURCE_DATABASE_ID_DEFAULT = "3db284de-cb43-80ed-9b6f-fc20d6cc20eb"
DESTINATION_DATABASE_TITLE = "Cat VisionLink History"
BUSINESS_TIMEZONE = "America/Los_Angeles"
SCHEDULED_SLOTS = ("10:00",)


class ConfigError(Exception):
    """The process cannot start with the current environment."""


@dataclass(frozen=True)
class Config:
    notion_token: str
    source_database_id: str
    destination_database_title: str
    destination_database_id: str | None
    dry_run: bool
    snapshot_slot: str | None
    snapshot_run_id: str | None
    business_timezone: str
    notion_version: str
    version: str
    cloud_run_execution: str | None

    def __repr__(self) -> str:
        return (
            "Config("
            f"version={self.version!r}, "
            f"dry_run={self.dry_run!r}, "
            f"source_database_id={self.source_database_id!r}, "
            f"destination_database_title={self.destination_database_title!r}, "
            f"destination_database_id={self.destination_database_id!r}, "
            f"snapshot_slot={self.snapshot_slot!r}, "
            f"snapshot_run_id={self.snapshot_run_id!r}, "
            f"business_timezone={self.business_timezone!r}, "
            f"cloud_run_execution={self.cloud_run_execution!r}, "
            "notion_token='[redacted]')"
        )


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y"}:
        return True
    if normalized in {"0", "false", "no", "n"}:
        return False
    raise ConfigError(f"DRY_RUN must be true or false, got {value!r}")


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def load_config(environ: dict[str, str] | None = None) -> Config:
    env = os.environ if environ is None else environ
    token = _blank_to_none(env.get("NOTION_TOKEN"))
    if not token:
        raise ConfigError("NOTION_TOKEN is not set")

    timezone_name = _blank_to_none(env.get("BUSINESS_TIMEZONE")) or BUSINESS_TIMEZONE
    try:
        ZoneInfo(timezone_name)
    except Exception as exc:  # zoneinfo raises ZoneInfoNotFoundError, a KeyError subclass
        raise ConfigError(f"Unknown BUSINESS_TIMEZONE {timezone_name!r}") from exc

    slot = _blank_to_none(env.get("SNAPSHOT_SLOT"))
    if slot is not None and slot not in SCHEDULED_SLOTS:
        allowed = ", ".join(SCHEDULED_SLOTS)
        raise ConfigError(f"SNAPSHOT_SLOT must be one of {allowed}, got {slot!r}")

    source_id = _blank_to_none(env.get("SOURCE_DATABASE_ID")) or SOURCE_DATABASE_ID_DEFAULT
    return Config(
        notion_token=token,
        source_database_id=source_id,
        destination_database_title=(
            _blank_to_none(env.get("DESTINATION_DATABASE_TITLE")) or DESTINATION_DATABASE_TITLE
        ),
        destination_database_id=_blank_to_none(env.get("DESTINATION_DATABASE_ID")),
        dry_run=_parse_bool(env.get("DRY_RUN"), default=False),
        snapshot_slot=slot,
        snapshot_run_id=_blank_to_none(env.get("SNAPSHOT_RUN_ID")),
        business_timezone=timezone_name,
        notion_version=_blank_to_none(env.get("NOTION_VERSION")) or "2022-06-28",
        version=VERSION,
        cloud_run_execution=_blank_to_none(env.get("CLOUD_RUN_EXECUTION")),
    )
