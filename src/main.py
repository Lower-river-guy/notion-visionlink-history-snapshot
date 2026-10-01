"""Cloud Run Job entrypoint for the VisionLink history snapshot."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from typing import Any

from src.config import VERSION, Config, ConfigError, load_config
from src.notion_client import NotionClient, redact_secrets
from src.snapshot import SnapshotFatal, format_summary, run_snapshot

_REDACT_KEYS = {"notion_token", "authorization", "token", "secret"}


def emit(severity: str, message: str, **fields: Any) -> None:
    record = {
        "severity": severity,
        "message": redact_secrets(message),
        "version": VERSION,
    }
    for key, value in fields.items():
        if key.lower() in _REDACT_KEYS:
            continue
        record[key] = _redact(value)
    print(json.dumps(record, default=str, ensure_ascii=False), flush=True)


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {
            key: _redact(item)
            for key, item in value.items()
            if key.lower() not in _REDACT_KEYS
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def run_job(
    config: Config,
    client: Any,
    *,
    now: datetime | None = None,
    logger: Any = None,
) -> int:
    log = logger or emit
    print("VisionLink History Snapshot starting", flush=True)
    print(f"Version: {config.version}", flush=True)
    log(
        "INFO",
        "VisionLink History Snapshot starting",
        version=config.version,
        dryRun=config.dry_run,
    )
    try:
        result = run_snapshot(config, client, log, now=now)
    except SnapshotFatal as exc:
        log(
            "ERROR",
            str(exc),
            status="FAILURE",
            databaseRole=exc.database_role,
            databaseId=exc.database_id,
            version=config.version,
        )
        print("VisionLink History Snapshot", flush=True)
        print(f"Version: {config.version}", flush=True)
        print("Status: FAILURE", flush=True)
        print(str(exc), flush=True)
        return 1

    summary = format_summary(result)
    print(summary, flush=True)
    log(
        "INFO" if result.status == "SUCCESS" else "ERROR",
        "snapshot finished",
        version=result.version,
        runId=result.run_id,
        snapshotTimestamp=result.snapshot_timestamp,
        sourceRecords=result.source_records,
        historyCreated=result.history_created,
        duplicatesSkipped=result.duplicates_skipped,
        recordsFailed=result.failed,
        durationSeconds=result.duration_seconds,
        status=result.status,
        dryRun=result.dry_run,
        wouldCreate=result.would_create,
        writesPerformed=result.writes_performed,
        sourceDatabaseId=result.source_database_id,
        sourceDatabaseTitle=result.source_database_title,
        destinationDatabaseId=result.destination_database_id,
        destinationDatabaseTitle=result.destination_database_title,
        unmappedSourceFields=result.unmapped_source_fields,
        schemaProblems=result.schema_problems,
        failedMachineIds=result.failed_machine_ids,
        coordinatesPreservedViaMap=result.coordinates_preserved_via_map,
    )
    return 0 if result.status == "SUCCESS" else 1


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        emit("ERROR", str(exc), status="FAILURE")
        print(f"VisionLink History Snapshot\nVersion: {VERSION}\nStatus: FAILURE\n{exc}", flush=True)
        return 1
    client = NotionClient(config.notion_token, notion_version=config.notion_version)
    return run_job(config, client)


if __name__ == "__main__":
    sys.exit(main())
