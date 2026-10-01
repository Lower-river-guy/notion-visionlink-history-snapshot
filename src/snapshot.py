"""Copy Cat VisionLink into append-only Cat VisionLink History rows.

The only write is creating a new history page. Previous snapshots are never
updated. Machines and Works Manager relations are never written.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from src.config import DESTINATION_DATABASE_TITLE, SCHEDULED_SLOTS, Config
from src.models import FieldMapping, HistoryDraft, SchemaPlan, SnapshotResult
from src.notion_client import (
    NotionError,
    database_title,
    normalize_notion_id,
    same_notion_id,
)

Logger = Callable[..., None]

# Source property -> history property. Types were read from the live schemas
# before this mapping was written. Relation fields are intentionally absent.
FIELD_SPECS: tuple[tuple[str, str, str], ...] = (
    ("Machine ID", "Machine ID", "title"),
    ("Hours", "Hours", "number"),
    ("Location", "Location", "rich_text"),
    ("Last Reported", "Last Reported", "date"),
    ("Fuel %", "Fuel %", "number"),
    ("Job Number", "Job Number", "rich_text"),
    ("Make", "Make", "rich_text"),
    ("Map", "Map", "url"),
    ("Model", "Model", "rich_text"),
    ("Serial Number", "Serial Number", "rich_text"),
    ("Status", "Status", "select"),
    ("Equipment Type", "Equipment Type", "select"),
)

DERIVED_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("Snapshot Date", "date", "Cloud Run execution time in the business timezone"),
    ("Snapshot Run ID", "rich_text", "Stable id for this scheduled slot or manual execution"),
)

# Destination properties that point at Machines or Works Manager projects.
# Setting them would write the other side of a two-way relation.
NEVER_WRITE_DESTINATION = {
    "Machine",
    "Related to Projects (VisionLink History)",
}

REQUIRED_DESTINATION = {
    "Machine ID": "title",
    "Snapshot Date": "date",
    "Snapshot Run ID": "rich_text",
    "Last Reported": "date",
}

_MAP_COORDINATES = re.compile(
    r"query=(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)"
)
_NOTION_DATE = re.compile(
    r"^\d{4}-\d{2}-\d{2}"
    r"(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?)?$"
)
_TEXT_LIMIT = 2000


class SnapshotFatal(Exception):
    """The snapshot cannot start or cannot finish safely."""

    def __init__(
        self,
        message: str,
        *,
        database_role: str | None = None,
        database_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.database_role = database_role
        self.database_id = database_id


def resolve_run_id(
    now: datetime,
    *,
    slot: str | None,
    execution: str | None,
    override: str | None,
    timezone_name: str = "America/Los_Angeles",
) -> str:
    """Build a retry-stable run id.

    Scheduled runs set SNAPSHOT_SLOT to 06:00 or 14:00. The id is that slot's
    civil time in the business timezone, so a retry later the same day still
    matches and a later slot does not.

    Manual runs (no slot) use the Cloud Run execution name, which is stable
    across task retries and different for a later execution. They never use a
    slot id, so an off-schedule run does not collide with 6:00 AM or 2:00 PM.
    """

    if override:
        return override
    if slot is not None:
        if slot not in SCHEDULED_SLOTS:
            raise SnapshotFatal(f"SNAPSHOT_SLOT {slot!r} is not a known weekday slot")
        anchored = anchor_slot(now, slot, timezone_name)
        return format_run_id(anchored, timezone_name)
    zone_label = timezone_name.replace("/", "-")
    if execution:
        safe_execution = re.sub(r"[^A-Za-z0-9_-]", "-", execution)[:200]
        return f"manual-{safe_execution}_{zone_label}"
    local = now.astimezone(ZoneInfo(timezone_name)).replace(microsecond=0)
    return "manual-" + format_run_id(local, timezone_name)


def anchor_slot(now: datetime, slot: str, timezone_name: str) -> datetime:
    """Latest occurrence of slot HH:MM in timezone_name that is not after now."""

    hour_text, minute_text = slot.split(":")
    hour, minute = int(hour_text), int(minute_text)
    zone = ZoneInfo(timezone_name)
    local = now.astimezone(zone)
    slot_date = local.date()
    candidate = datetime(slot_date.year, slot_date.month, slot_date.day, hour, minute, tzinfo=zone)
    if local < candidate:
        yesterday = slot_date - timedelta(days=1)
        candidate = datetime(
            yesterday.year,
            yesterday.month,
            yesterday.day,
            hour,
            minute,
            tzinfo=zone,
        )
    return candidate


def format_run_id(local_dt: datetime, timezone_name: str) -> str:
    """Civil time plus the IANA zone name. The offset is not embedded."""

    zone_label = timezone_name.replace("/", "-")
    return local_dt.strftime("%Y-%m-%dT%H:%M:%S") + f"_{zone_label}"


def run_snapshot(
    config: Config,
    client: Any,
    logger: Logger,
    *,
    now: datetime | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    pause_seconds: float = 0.2,
) -> SnapshotResult:
    started = time.perf_counter()
    zone = ZoneInfo(config.business_timezone)
    moment = now or datetime.now(zone)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=zone)
    snapshot_local = moment.astimezone(zone)
    run_id = resolve_run_id(
        moment,
        slot=config.snapshot_slot,
        execution=config.cloud_run_execution,
        override=config.snapshot_run_id,
        timezone_name=config.business_timezone,
    )
    snapshot_timestamp = snapshot_local.isoformat(timespec="seconds")

    logger(
        "INFO",
        "snapshot starting",
        runId=run_id,
        snapshotTimestamp=snapshot_timestamp,
        dryRun=config.dry_run,
        version=config.version,
    )

    source = _retrieve_role(client, "source", config.source_database_id)
    source_title = database_title(source)
    if "VisionLink" not in source_title or source_title == config.destination_database_title:
        raise SnapshotFatal(
            f"Source database title {source_title!r} is not the VisionLink source",
            database_role="source",
            database_id=config.source_database_id,
        )

    destination_id = _resolve_destination_id(config, client)
    if same_notion_id(destination_id, config.source_database_id):
        raise SnapshotFatal(
            "Destination database id matches the source database; refusing to continue",
            database_role="destination",
            database_id=destination_id,
        )
    destination = _retrieve_role(client, "destination", destination_id)
    destination_title = database_title(destination)
    if destination_title != config.destination_database_title:
        raise SnapshotFatal(
            f"Destination database title {destination_title!r} "
            f"does not equal {config.destination_database_title!r}",
            database_role="destination",
            database_id=destination_id,
        )

    plan = build_schema_plan(source, destination)
    if plan.schema_problems:
        logger(
            "WARNING",
            "schema differences",
            schemaProblems=plan.schema_problems,
            runId=run_id,
        )
    logger(
        "INFO",
        "databases detected",
        sourceDatabaseId=plan.source_database_id,
        sourceDatabaseTitle=plan.source_database_title,
        destinationDatabaseId=plan.destination_database_id,
        destinationDatabaseTitle=plan.destination_database_title,
        mappings=[
            {
                "sourceProperty": item.source_property,
                "sourceType": item.source_type,
                "historyProperty": item.history_property,
                "historyType": item.history_type,
            }
            for item in plan.mappings
        ],
        unmappedSourceFields=plan.unmapped_source_fields,
        untouchedDestinationFields=plan.untouched_destination_fields,
        runId=run_id,
    )

    try:
        source_pages = client.query_database(plan.source_database_id)
    except NotionError as exc:
        raise SnapshotFatal(
            f"Cannot read source database {plan.source_database_id}: {exc}",
            database_role="source",
            database_id=plan.source_database_id,
        ) from exc

    drafts = [
        build_history_draft(
            page,
            plan,
            snapshot_local=snapshot_local,
            run_id=run_id,
            timezone_name=config.business_timezone,
        )
        for page in source_pages
    ]

    try:
        existing_machine_ids = _existing_machine_ids(client, plan.destination_database_id, run_id)
    except NotionError as exc:
        raise SnapshotFatal(
            f"Cannot read destination database {plan.destination_database_id}: {exc}",
            database_role="destination",
            database_id=plan.destination_database_id,
        ) from exc

    created = 0
    skipped = 0
    failed = 0
    would_create = 0
    failed_ids: list[str] = []
    seen: set[str] = set()
    coordinates = 0

    for draft in drafts:
        if draft.coordinates is not None:
            coordinates += 1
        label = draft.machine_id or draft.source_page_id
        if draft.errors or not draft.machine_id:
            failed += 1
            failed_ids.append(label)
            logger(
                "ERROR",
                "machine snapshot failed",
                machineId=draft.machine_id,
                sourcePageId=draft.source_page_id,
                error="; ".join(draft.errors) or "missing Machine ID",
                runId=run_id,
            )
            continue
        if draft.warnings:
            logger(
                "WARNING",
                "machine snapshot warnings",
                machineId=draft.machine_id,
                warnings=draft.warnings,
                runId=run_id,
            )
        if draft.machine_id in existing_machine_ids or draft.machine_id in seen:
            skipped += 1
            logger(
                "INFO",
                "duplicate skipped",
                machineId=draft.machine_id,
                runId=run_id,
            )
            continue
        seen.add(draft.machine_id)
        if config.dry_run:
            would_create += 1
            logger(
                "INFO",
                "dry run would create history record",
                machineId=draft.machine_id,
                runId=run_id,
                snapshotTimestamp=snapshot_timestamp,
                lastReported=draft.last_reported_start,
                properties=_loggable_properties(draft.properties),
            )
            continue
        try:
            client.create_page(
                plan.destination_database_id,
                draft.properties,
                forbidden_database_ids=(plan.source_database_id,),
            )
        except NotionError as exc:
            failed += 1
            failed_ids.append(draft.machine_id)
            logger(
                "ERROR",
                "machine snapshot failed",
                machineId=draft.machine_id,
                sourcePageId=draft.source_page_id,
                error=str(exc),
                runId=run_id,
            )
            continue
        created += 1
        existing_machine_ids.add(draft.machine_id)
        if pause_seconds:
            sleeper(pause_seconds)

    status = "SUCCESS" if failed == 0 else "FAILURE"
    result = SnapshotResult(
        version=config.version,
        run_id=run_id,
        snapshot_timestamp=snapshot_timestamp,
        source_records=len(source_pages),
        history_created=created,
        duplicates_skipped=skipped,
        failed=failed,
        dry_run=config.dry_run,
        would_create=would_create,
        status=status,
        duration_seconds=round(time.perf_counter() - started, 3),
        source_database_id=plan.source_database_id,
        source_database_title=plan.source_database_title,
        destination_database_id=plan.destination_database_id,
        destination_database_title=plan.destination_database_title,
        mappings=plan.mappings,
        unmapped_source_fields=plan.unmapped_source_fields,
        untouched_destination_fields=plan.untouched_destination_fields,
        schema_problems=plan.schema_problems,
        failed_machine_ids=failed_ids,
        coordinates_preserved_via_map=coordinates,
        writes_performed=created,
    )
    return result


def build_schema_plan(source: dict[str, Any], destination: dict[str, Any]) -> SchemaPlan:
    source_props = source.get("properties") or {}
    dest_props = destination.get("properties") or {}
    problems: list[str] = []
    mappings: list[FieldMapping] = []
    select_options: dict[str, set[str]] = {}
    mapped_source: set[str] = set()

    fatal_missing = []
    for name, expected_type in REQUIRED_DESTINATION.items():
        prop = dest_props.get(name)
        actual = prop.get("type") if isinstance(prop, dict) else None
        if actual != expected_type:
            fatal_missing.append(f"{name} (expected {expected_type}, found {actual})")
    if fatal_missing:
        raise SnapshotFatal(
            "Destination schema is missing required properties: " + ", ".join(fatal_missing),
            database_role="destination",
            database_id=destination.get("id"),
        )

    for source_name, dest_name, kind in FIELD_SPECS:
        mapped_source.add(source_name)
        source_prop = source_props.get(source_name)
        dest_prop = dest_props.get(dest_name)
        if not isinstance(source_prop, dict):
            problems.append(f"Source property {source_name!r} was not found")
            continue
        if not isinstance(dest_prop, dict):
            problems.append(
                f"History property {dest_name!r} was not found; {source_name!r} will not be copied"
            )
            continue
        source_type = source_prop.get("type")
        dest_type = dest_prop.get("type")
        if source_type != kind or dest_type != kind:
            problems.append(
                f"{source_name} ({source_type}) -> {dest_name} ({dest_type}) "
                f"does not match expected type {kind}"
            )
            continue
        if dest_name in NEVER_WRITE_DESTINATION or dest_type == "relation":
            problems.append(f"Refusing to map {dest_name!r} because it must not be written")
            continue
        mappings.append(
            FieldMapping(
                source_property=source_name,
                source_type=str(source_type),
                history_property=dest_name,
                history_type=str(dest_type),
            )
        )
        if kind == "select":
            options = {
                option.get("name")
                for option in (dest_prop.get("select") or {}).get("options") or []
                if option.get("name")
            }
            select_options[dest_name] = options
            source_options = {
                option.get("name")
                for option in (source_prop.get("select") or {}).get("options") or []
                if option.get("name")
            }
            missing = sorted(source_options - options)
            if missing:
                problems.append(
                    f"Destination select {dest_name!r} is missing source options: "
                    + ", ".join(missing)
                    + ". A machine that uses one of those values fails without a write."
                )

    for dest_name, dest_type, description in DERIVED_FIELDS:
        dest_prop = dest_props.get(dest_name)
        actual = dest_prop.get("type") if isinstance(dest_prop, dict) else None
        if actual != dest_type:
            problems.append(
                f"Derived history property {dest_name!r} type {actual} != {dest_type} ({description})"
            )

    if "Latitude" not in dest_props or "Longitude" not in dest_props:
        problems.append(
            "Latitude and Longitude are not history properties. "
            "Coordinates encoded in the source Map URL are preserved by copying Map."
        )

    unmapped = sorted(name for name in source_props if name not in mapped_source)
    mapped_dest = {item.history_property for item in mappings}
    derived_dest = {name for name, _, _ in DERIVED_FIELDS}
    untouched = sorted(
        name for name in dest_props if name not in mapped_dest and name not in derived_dest
    )
    for name in NEVER_WRITE_DESTINATION:
        if name in dest_props and name not in untouched:
            untouched.append(name)
    relation_sources = [
        name
        for name in unmapped
        if isinstance(source_props.get(name), dict) and source_props[name].get("type") == "relation"
    ]
    if relation_sources:
        problems.append(
            "Source relation fields are not copied (this job does not write Machines "
            "or Works Manager): " + ", ".join(relation_sources)
        )

    return SchemaPlan(
        source_database_id=normalize_notion_id(str(source.get("id"))),
        source_database_title=database_title(source),
        destination_database_id=normalize_notion_id(str(destination.get("id"))),
        destination_database_title=database_title(destination),
        mappings=mappings,
        unmapped_source_fields=unmapped,
        untouched_destination_fields=untouched,
        schema_problems=problems,
        select_options=select_options,
    )


def build_history_draft(
    page: dict[str, Any],
    plan: SchemaPlan,
    *,
    snapshot_local: datetime,
    run_id: str,
    timezone_name: str,
) -> HistoryDraft:
    properties_in = page.get("properties") or {}
    errors: list[str] = []
    warnings: list[str] = []
    properties: dict[str, Any] = {}

    machine_prop = properties_in.get("Machine ID")
    try:
        machine_id = _plain_text(machine_prop)
    except ValueError as exc:
        machine_id = None
        errors.append(f"Machine ID: {exc}")
    if not machine_id:
        errors.append("Machine ID is missing")

    last_reported_start: str | None = None
    coordinates: tuple[float, float] | None = None

    for mapping in plan.mappings:
        if mapping.source_property == "Machine ID":
            continue
        raw = properties_in.get(mapping.source_property)
        try:
            if mapping.history_type == "rich_text":
                text = _plain_text(raw)
                if text is None:
                    continue
                if len(text) > _TEXT_LIMIT:
                    warnings.append(
                        f"{mapping.source_property} truncated from {len(text)} to {_TEXT_LIMIT} characters"
                    )
                    text = text[:_TEXT_LIMIT]
                properties[mapping.history_property] = _rich_text(text)
            elif mapping.history_type == "number":
                number = _number(raw)
                if number is None:
                    continue
                properties[mapping.history_property] = {"number": number}
            elif mapping.history_type == "url":
                url = _url(raw)
                if url is None:
                    continue
                properties[mapping.history_property] = {"url": url}
                if mapping.source_property == "Map":
                    coordinates = _coordinates(url)
            elif mapping.history_type == "select":
                selected = _select_name(raw)
                if selected is None:
                    continue
                allowed = plan.select_options.get(mapping.history_property, set())
                if selected not in allowed:
                    errors.append(
                        f"{mapping.source_property} value {selected!r} is not an option "
                        f"on history property {mapping.history_property!r}"
                    )
                    continue
                properties[mapping.history_property] = {"select": {"name": selected}}
            elif mapping.history_type == "date":
                copied = _copy_date(raw)
                if copied is None:
                    continue
                properties[mapping.history_property] = copied
                if mapping.source_property == "Last Reported":
                    last_reported_start = copied["date"]["start"]
            else:
                errors.append(f"Unsupported history type {mapping.history_type}")
        except ValueError as exc:
            errors.append(f"{mapping.source_property}: {exc}")

    if machine_id and not errors:
        properties["Machine ID"] = _title(machine_id)
        properties["Snapshot Run ID"] = _rich_text(run_id)
        properties["Snapshot Date"] = _execution_date(snapshot_local, timezone_name)

    return HistoryDraft(
        machine_id=machine_id,
        source_page_id=str(page.get("id") or ""),
        properties=properties,
        errors=errors,
        warnings=warnings,
        last_reported_start=last_reported_start,
        coordinates=coordinates,
    )


def format_summary(result: SnapshotResult) -> str:
    lines = [
        "VisionLink History Snapshot",
        f"Version: {result.version}",
        f"Run ID: {result.run_id}",
        f"Source records: {result.source_records}",
        f"History created: {result.history_created}",
        f"Duplicates skipped: {result.duplicates_skipped}",
        f"Failed: {result.failed}",
        f"Status: {result.status}",
    ]
    if result.dry_run:
        lines.extend(
            [
                f"Would create: {result.would_create}",
                "DRY_RUN: true",
                "Writes performed: 0",
            ]
        )
    lines.append(f"Duration seconds: {result.duration_seconds:.3f}")
    return "\n".join(lines)


def _retrieve_role(client: Any, role: str, database_id: str) -> dict[str, Any]:
    try:
        return client.retrieve_database(database_id)
    except NotionError as exc:
        raise SnapshotFatal(
            f"Cannot access {role} database {database_id}: {exc}",
            database_role=role,
            database_id=database_id,
        ) from exc


def _resolve_destination_id(config: Config, client: Any) -> str:
    expected = config.destination_database_title or DESTINATION_DATABASE_TITLE
    try:
        matches = client.search_databases_by_title(expected)
    except NotionError as exc:
        raise SnapshotFatal(
            f"Cannot search for destination database {expected!r}: {exc}",
            database_role="destination",
        ) from exc
    ids = [normalize_notion_id(str(item.get("id"))) for item in matches if item.get("id")]
    unique_ids = list(dict.fromkeys(ids))
    if config.destination_database_id:
        configured = normalize_notion_id(config.destination_database_id)
        if unique_ids and not any(same_notion_id(configured, found) for found in unique_ids):
            raise SnapshotFatal(
                f"DESTINATION_DATABASE_ID {configured} is not the database titled {expected!r}. "
                f"Search found: {', '.join(unique_ids)}",
                database_role="destination",
                database_id=configured,
            )
        return configured
    if len(unique_ids) != 1:
        found = ", ".join(unique_ids) if unique_ids else "none"
        raise SnapshotFatal(
            f"Expected one destination database titled {expected!r}, found: {found}",
            database_role="destination",
        )
    return unique_ids[0]


def _existing_machine_ids(client: Any, database_id: str, run_id: str) -> set[str]:
    pages = client.query_database(
        database_id,
        filter_body={
            "property": "Snapshot Run ID",
            "rich_text": {"equals": run_id},
        },
    )
    found: set[str] = set()
    for page in pages:
        machine_id = _plain_text((page.get("properties") or {}).get("Machine ID"))
        if machine_id:
            found.add(machine_id)
    return found


def _plain_text(prop: Any) -> str | None:
    if not prop or not isinstance(prop, dict):
        return None
    kind = prop.get("type")
    if kind == "title":
        items = prop.get("title") or []
    elif kind == "rich_text":
        items = prop.get("rich_text") or []
    else:
        return None
    text = "".join(_item_text(item) for item in items if isinstance(item, dict)).strip()
    return text or None


def _item_text(item: dict) -> str:
    plain = item.get("plain_text")
    if plain:
        return str(plain)
    text = item.get("text")
    if isinstance(text, dict) and text.get("content"):
        return str(text["content"])
    return ""


def _number(prop: Any) -> int | float | None:
    if not prop or not isinstance(prop, dict) or prop.get("type") != "number":
        return None
    value = prop.get("number")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("value is not a number")
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        raise ValueError("value is not a finite number")
    return value


def _url(prop: Any) -> str | None:
    if not prop or not isinstance(prop, dict) or prop.get("type") != "url":
        return None
    value = prop.get("url")
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    if not (text.startswith("https://") or text.startswith("http://")):
        raise ValueError("URL must start with http:// or https://")
    return text


def _select_name(prop: Any) -> str | None:
    if not prop or not isinstance(prop, dict) or prop.get("type") != "select":
        return None
    selected = prop.get("select")
    if not isinstance(selected, dict):
        return None
    name = str(selected.get("name") or "").strip()
    return name or None


def _copy_date(prop: Any) -> dict[str, Any] | None:
    if not prop or not isinstance(prop, dict) or prop.get("type") != "date":
        return None
    date = prop.get("date")
    if not isinstance(date, dict) or not date.get("start"):
        return None
    start = str(date["start"]).strip()
    if not _NOTION_DATE.match(start):
        raise ValueError(f"malformed date {start!r}")
    payload: dict[str, Any] = {"start": start}
    end = date.get("end")
    if end:
        end_text = str(end).strip()
        if not _NOTION_DATE.match(end_text):
            raise ValueError(f"malformed date end {end_text!r}")
        payload["end"] = end_text
    time_zone = date.get("time_zone")
    if time_zone:
        payload["time_zone"] = time_zone
    return {"date": payload}


def _execution_date(snapshot_local: datetime, timezone_name: str) -> dict[str, Any]:
    local = snapshot_local.astimezone(ZoneInfo(timezone_name))
    return {
        "date": {
            "start": local.isoformat(timespec="seconds"),
            "time_zone": timezone_name,
        }
    }


def _title(value: str) -> dict[str, Any]:
    return {"title": [{"type": "text", "text": {"content": value}}]}


def _rich_text(value: str) -> dict[str, Any]:
    return {"rich_text": [{"type": "text", "text": {"content": value}}]}


def _coordinates(url: str) -> tuple[float, float] | None:
    match = _MAP_COORDINATES.search(url)
    if not match:
        return None
    return float(match.group(1)), float(match.group(2))


def _loggable_properties(properties: dict[str, Any]) -> dict[str, Any]:
    """Compact property values for logs. Tokens are not part of these payloads."""

    compact: dict[str, Any] = {}
    for name, value in properties.items():
        if not isinstance(value, dict):
            compact[name] = value
            continue
        if "title" in value:
            compact[name] = _plain_text({"type": "title", "title": value["title"]})
        elif "rich_text" in value:
            compact[name] = _plain_text({"type": "rich_text", "rich_text": value["rich_text"]})
        elif "number" in value:
            compact[name] = value["number"]
        elif "url" in value:
            compact[name] = value["url"]
        elif "select" in value:
            compact[name] = (value.get("select") or {}).get("name")
        elif "date" in value:
            compact[name] = value["date"]
        else:
            compact[name] = sorted(value.keys())
    return compact
