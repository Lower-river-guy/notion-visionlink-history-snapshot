"""Hour-meter deltas for Machines, computed from Cat VisionLink History.

Hours This Week, Hours Last Week, and Hours This Month are number properties
on Machines. Each value is the later meter minus the earlier meter. Readings
are never summed. Notion formulas are not used.

Periods are half-open and use America/Los_Angeles:

- this week: Monday 00:00 through the next Monday 00:00
- last week: the Monday before that, through this Monday 00:00
- this month: the first of the month 00:00 through the next month 00:00

A period is blank unless there is a reading strictly before it starts and a
reading inside it. A meter decrease, a duplicate whose hours disagree, or a
negative result blanks that period. Values are rounded half up to 0.1 hour.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable
from zoneinfo import ZoneInfo

from src.notion_client import (
    MACHINE_NUMBER_PROPERTIES,
    NotionError,
    normalize_notion_id,
)
from src.snapshot import SnapshotFatal, build_machine_index

HOURS_TOLERANCE = 0.05
Logger = Callable[..., None]


class UtilizationFatal(SnapshotFatal):
    """Utilization cannot run. Snapshot rows already written are left as they are."""


@dataclass(frozen=True)
class PeriodWindow:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class PeriodBoundaries:
    this_week: PeriodWindow
    last_week: PeriodWindow
    this_month: PeriodWindow
    as_of: datetime
    timezone: str


@dataclass(frozen=True)
class Reading:
    timestamp: datetime
    hours: float
    snapshot_run_id: str | None = None
    page_id: str | None = None


@dataclass(frozen=True)
class NormalizedHistory:
    readings: tuple[Reading, ...]
    duplicate_conflicts: tuple[datetime, ...]
    resets: tuple[datetime, ...]
    out_of_order: bool
    ignored_future: int
    dropped_incomplete: int


@dataclass(frozen=True)
class MachineUtilization:
    machine_id: str
    page_id: str | None
    hours_this_week: float | None
    hours_last_week: float | None
    hours_this_month: float | None


@dataclass
class UtilizationResult:
    machines: int
    updated: int
    skipped_duplicates: int
    failed: int
    dry_run: bool
    sample: list[dict[str, Any]]


def get_period_boundaries(
    now: datetime,
    timezone_name: str = "America/Los_Angeles",
) -> PeriodBoundaries:
    """Calendar week (Monday start) and calendar month around `now`."""

    zone = ZoneInfo(timezone_name)
    local = now.replace(tzinfo=zone) if now.tzinfo is None else now.astimezone(zone)
    local = local.replace(microsecond=0)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    monday = midnight - timedelta(days=midnight.weekday())
    next_monday = monday + timedelta(days=7)
    previous_monday = monday - timedelta(days=7)
    month_start = midnight.replace(day=1)
    if month_start.month == 12:
        next_month = month_start.replace(year=month_start.year + 1, month=1)
    else:
        next_month = month_start.replace(month=month_start.month + 1)
    return PeriodBoundaries(
        this_week=PeriodWindow(monday, next_monday),
        last_week=PeriodWindow(previous_monday, monday),
        this_month=PeriodWindow(month_start, next_month),
        as_of=local,
        timezone=timezone_name,
    )


def normalize_history(
    readings: list[Reading],
    *,
    now: datetime | None = None,
    timezone_name: str = "America/Los_Angeles",
) -> NormalizedHistory:
    """Sort readings, drop future rows, and collapse exact duplicates.

    Rows that share a timestamp to the second, or share a Snapshot Run ID,
    are one reading when their hours agree. Disagreeing hours are a conflict
    and none of that group is kept.
    """

    zone = ZoneInfo(timezone_name)
    as_of = None
    if now is not None:
        as_of = now.replace(tzinfo=zone) if now.tzinfo is None else now.astimezone(zone)

    prepared: list[Reading] = []
    dropped = 0
    ignored_future = 0
    for reading in readings:
        if reading.timestamp is None or reading.hours is None:
            dropped += 1
            continue
        if isinstance(reading.hours, bool) or not isinstance(reading.hours, (int, float)):
            dropped += 1
            continue
        hours = float(reading.hours)
        if hours != hours or hours in (float("inf"), float("-inf")):
            dropped += 1
            continue
        timestamp = reading.timestamp
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=zone)
        else:
            timestamp = timestamp.astimezone(zone)
        timestamp = timestamp.replace(microsecond=0)
        if as_of is not None and timestamp > as_of:
            ignored_future += 1
            continue
        run_id = (reading.snapshot_run_id or "").strip() or None
        prepared.append(
            Reading(
                timestamp=timestamp,
                hours=hours,
                snapshot_run_id=run_id,
                page_id=reading.page_id,
            )
        )

    out_of_order = any(
        prepared[index].timestamp > prepared[index + 1].timestamp
        for index in range(len(prepared) - 1)
    )
    prepared.sort(key=lambda item: (item.timestamp, item.page_id or ""))
    kept, conflicts = _collapse_duplicates(prepared)
    resets: list[datetime] = []
    for previous, current in zip(kept, kept[1:]):
        if current.hours < previous.hours - HOURS_TOLERANCE:
            resets.append(current.timestamp)
    return NormalizedHistory(
        readings=tuple(kept),
        duplicate_conflicts=tuple(conflicts),
        resets=tuple(resets),
        out_of_order=out_of_order,
        ignored_future=ignored_future,
        dropped_incomplete=dropped,
    )


def calculate_period_hours(
    history: NormalizedHistory,
    start: datetime,
    end: datetime,
) -> float | None:
    """Later meter minus earlier meter for [start, end). Blank when unusable."""

    baseline: Reading | None = None
    end_reading: Reading | None = None
    for reading in history.readings:
        if reading.timestamp < start:
            baseline = reading
        elif reading.timestamp < end:
            end_reading = reading
    if baseline is None or end_reading is None:
        return None
    if end_reading.timestamp <= baseline.timestamp:
        return None
    for conflict_at in history.duplicate_conflicts:
        if baseline.timestamp <= conflict_at <= end_reading.timestamp:
            return None
    for reset_at in history.resets:
        if baseline.timestamp < reset_at <= end_reading.timestamp:
            return None
    delta = end_reading.hours - baseline.hours
    if delta < -HOURS_TOLERANCE:
        return None
    if delta < 0:
        delta = 0.0
    return _round_hours(delta)


def calculate_machine_utilization(
    machine_id: str,
    readings: list[Reading],
    now: datetime,
    *,
    page_id: str | None = None,
    timezone_name: str = "America/Los_Angeles",
) -> MachineUtilization:
    boundaries = get_period_boundaries(now, timezone_name)
    history = normalize_history(readings, now=now, timezone_name=timezone_name)
    return MachineUtilization(
        machine_id=machine_id,
        page_id=page_id,
        hours_this_week=calculate_period_hours(
            history, boundaries.this_week.start, boundaries.this_week.end
        ),
        hours_last_week=calculate_period_hours(
            history, boundaries.last_week.start, boundaries.last_week.end
        ),
        hours_this_month=calculate_period_hours(
            history, boundaries.this_month.start, boundaries.this_month.end
        ),
    )


def update_machine_utilization(
    client: Any,
    page_id: str,
    utilization: MachineUtilization,
    *,
    dry_run: bool,
    logger: Logger,
) -> bool:
    """Write the three number fields. Dry run logs the values and writes nothing."""

    properties = {
        "Hours This Week": {"number": utilization.hours_this_week},
        "Hours Last Week": {"number": utilization.hours_last_week},
        "Hours This Month": {"number": utilization.hours_this_month},
    }
    logger(
        "INFO",
        "machine utilization",
        machineId=utilization.machine_id,
        pageId=page_id,
        dryRun=dry_run,
        hoursThisWeek=utilization.hours_this_week,
        hoursLastWeek=utilization.hours_last_week,
        hoursThisMonth=utilization.hours_this_month,
        wouldWrite=not dry_run,
    )
    if dry_run:
        return False
    client.update_machine_numbers(page_id, properties)
    return True


def run_utilization(
    config: Any,
    client: Any,
    logger: Logger,
    *,
    now: datetime,
    destination_database_id: str,
    pause_seconds: float = 0.2,
    sleeper: Callable[[float], None] | None = None,
) -> UtilizationResult:
    """Read history, compute deltas, and update only the three Machines numbers."""

    import time

    machines_id = normalize_notion_id(config.machines_database_id)
    try:
        machines_db = client.retrieve_database(machines_id)
    except NotionError as exc:
        raise UtilizationFatal(
            f"Cannot read Machines database {machines_id}: {exc}",
            database_role="machines",
            database_id=machines_id,
        ) from exc
    _require_number_properties(machines_db, machines_id)

    try:
        machine_pages = client.query_database(machines_id)
        history_pages = client.query_database(destination_database_id)
    except NotionError as exc:
        raise UtilizationFatal(
            f"Cannot read history for utilization: {exc}",
            database_role="destination",
            database_id=destination_database_id,
        ) from exc

    unique, duplicates = build_machine_index(machine_pages)
    for machine_id, page_ids in duplicates.items():
        logger(
            "WARNING",
            "duplicate Machines id skipped for utilization",
            machineId=machine_id,
            pageIds=page_ids,
        )

    grouped: dict[str, list[Reading]] = {}
    for page in history_pages:
        parsed = reading_from_history_page(page, config.business_timezone)
        if parsed is None:
            continue
        machine_id, reading = parsed
        grouped.setdefault(machine_id, []).append(reading)

    updated = 0
    failed = 0
    sample: list[dict[str, Any]] = []
    pause = sleeper or time.sleep
    for machine_id, page_id in unique.items():
        utilization = calculate_machine_utilization(
            machine_id,
            grouped.get(machine_id, []),
            now,
            page_id=page_id,
            timezone_name=config.business_timezone,
        )
        if len(sample) < 8 and _has_value(utilization):
            sample.append(_sample_row(utilization))
        try:
            wrote = update_machine_utilization(
                client,
                page_id,
                utilization,
                dry_run=config.dry_run,
                logger=logger,
            )
        except NotionError as exc:
            failed += 1
            logger(
                "ERROR",
                "machine utilization update failed",
                machineId=machine_id,
                pageId=page_id,
                notionStatus=getattr(exc, "status_code", None),
                error=str(exc),
            )
            continue
        if wrote:
            updated += 1
            if pause_seconds:
                pause(pause_seconds)
    if len(sample) < 8:
        for machine_id, page_id in unique.items():
            if len(sample) >= 8:
                break
            if any(row["machineId"] == machine_id for row in sample):
                continue
            utilization = calculate_machine_utilization(
                machine_id,
                grouped.get(machine_id, []),
                now,
                page_id=page_id,
                timezone_name=config.business_timezone,
            )
            sample.append(_sample_row(utilization))

    logger(
        "INFO",
        "utilization finished",
        dryRun=config.dry_run,
        machines=len(unique),
        updated=updated,
        skippedDuplicateMachineIds=len(duplicates),
        failed=failed,
        sample=sample,
    )
    return UtilizationResult(
        machines=len(unique),
        updated=updated,
        skipped_duplicates=len(duplicates),
        failed=failed,
        dry_run=config.dry_run,
        sample=sample,
    )


def reading_from_history_page(
    page: dict[str, Any],
    timezone_name: str,
) -> tuple[str, Reading] | None:
    """One history row. None when Machine ID, Hours, or Snapshot Date is missing."""

    properties = page.get("properties") or {}
    machine_id = _plain_text(properties.get("Machine ID"))
    if not machine_id:
        return None
    hours = _number(properties.get("Hours"))
    if hours is None:
        return None
    timestamp = _snapshot_timestamp(properties.get("Snapshot Date"), timezone_name)
    if timestamp is None:
        return None
    return machine_id, Reading(
        timestamp=timestamp,
        hours=float(hours),
        snapshot_run_id=_plain_text(properties.get("Snapshot Run ID")),
        page_id=str(page.get("id") or "") or None,
    )


def _collapse_duplicates(readings: list[Reading]) -> tuple[list[Reading], list[datetime]]:
    if not readings:
        return [], []
    parent = list(range(len(readings)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    by_second: dict[datetime, int] = {}
    by_run: dict[str, int] = {}
    for index, reading in enumerate(readings):
        second = reading.timestamp.replace(microsecond=0)
        if second in by_second:
            union(index, by_second[second])
        else:
            by_second[second] = index
        if reading.snapshot_run_id:
            previous = by_run.get(reading.snapshot_run_id)
            if previous is None:
                by_run[reading.snapshot_run_id] = index
            else:
                union(index, previous)

    groups: dict[int, list[Reading]] = {}
    for index, reading in enumerate(readings):
        groups.setdefault(find(index), []).append(reading)

    kept: list[Reading] = []
    conflicts: list[datetime] = []
    for group in groups.values():
        hours = [item.hours for item in group]
        if max(hours) - min(hours) > HOURS_TOLERANCE:
            conflicts.append(min(item.timestamp for item in group))
            continue
        kept.append(min(group, key=lambda item: (item.timestamp, item.page_id or "")))
    kept.sort(key=lambda item: (item.timestamp, item.page_id or ""))
    conflicts.sort()
    return kept, conflicts


def _round_hours(value: float) -> float:
    quantized = Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return float(quantized)


def _require_number_properties(database: dict[str, Any], database_id: str) -> None:
    properties = database.get("properties") or {}
    missing = []
    for name in MACHINE_NUMBER_PROPERTIES:
        prop = properties.get(name)
        if not isinstance(prop, dict) or prop.get("type") != "number":
            missing.append(name)
    if missing:
        joined = ", ".join(sorted(missing))
        raise UtilizationFatal(
            f"Machines is missing number properties: {joined}",
            database_role="machines",
            database_id=database_id,
        )


def _has_value(utilization: MachineUtilization) -> bool:
    return any(
        value is not None
        for value in (
            utilization.hours_this_week,
            utilization.hours_last_week,
            utilization.hours_this_month,
        )
    )


def _sample_row(utilization: MachineUtilization) -> dict[str, Any]:
    return {
        "machineId": utilization.machine_id,
        "hoursThisWeek": utilization.hours_this_week,
        "hoursLastWeek": utilization.hours_last_week,
        "hoursThisMonth": utilization.hours_this_month,
    }


def _plain_text(prop: Any) -> str | None:
    if not isinstance(prop, dict):
        return None
    kind = prop.get("type")
    if kind == "title":
        items = prop.get("title") or []
    elif kind == "rich_text":
        items = prop.get("rich_text") or []
    else:
        return None
    text = "".join(
        str(item.get("plain_text") or "")
        for item in items
        if isinstance(item, dict)
    ).strip()
    return text or None


def _number(prop: Any) -> float | None:
    if not isinstance(prop, dict) or prop.get("type") != "number":
        return None
    value = prop.get("number")
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return None
    return float(value)


def _snapshot_timestamp(prop: Any, timezone_name: str) -> datetime | None:
    if not isinstance(prop, dict) or prop.get("type") != "date":
        return None
    date = prop.get("date")
    if not isinstance(date, dict) or not date.get("start"):
        return None
    return _parse_history_instant(str(date["start"]), date.get("time_zone"), timezone_name)


def _parse_history_instant(
    start: str,
    time_zone: str | None,
    business_timezone: str,
) -> datetime | None:
    text = start.strip()
    if not text:
        return None
    zone_name = str(time_zone or "").strip() or None
    business = ZoneInfo(business_timezone)
    if "T" not in text:
        try:
            civil = datetime.fromisoformat(text)
        except ValueError:
            return None
        return civil.replace(tzinfo=business)
    if zone_name and _has_numeric_offset(text):
        instant = _parse_offset_datetime(text)
        if instant is None:
            return None
        local = instant.astimezone(ZoneInfo(zone_name)).replace(tzinfo=None)
        return local.replace(tzinfo=ZoneInfo(zone_name)).astimezone(business)
    if zone_name:
        naive = text[:-1] if text.endswith("Z") else text
        try:
            civil = datetime.fromisoformat(naive)
        except ValueError:
            return None
        return civil.replace(tzinfo=ZoneInfo(zone_name)).astimezone(business)
    if _has_numeric_offset(text) or text.endswith("Z"):
        instant = _parse_offset_datetime(text)
        if instant is None:
            return None
        return instant.astimezone(business)
    try:
        civil = datetime.fromisoformat(text)
    except ValueError:
        return None
    return civil.replace(tzinfo=business)


def _has_numeric_offset(text: str) -> bool:
    if text.endswith("Z"):
        return True
    if len(text) < 6 or text[-3] != ":":
        return False
    sign = text[-6]
    return sign in "+-" and text[-5:-3].isdigit() and text[-2:].isdigit()


def _parse_offset_datetime(text: str) -> datetime | None:
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed
