"""Mapping, idempotency, dry run, and per-machine failure behavior."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from src.config import Config
from src.snapshot import resolve_run_id, run_snapshot
from tests.fakes import FakeNotion
from tests.notion_fixtures import history_database, source_database, source_page

LA = ZoneInfo("America/Los_Angeles")
EXECUTION = datetime(2026, 10, 1, 17, 5, 11, tzinfo=timezone.utc)  # 10:05:11 PDT


def _config(**overrides):
    values = dict(
        notion_token="secret_testtokenvalue",
        source_database_id="3db284de-cb43-80ed-9b6f-fc20d6cc20eb",
        destination_database_title="Cat VisionLink History",
        destination_database_id=None,
        dry_run=False,
        snapshot_slot="10:00",
        snapshot_run_id=None,
        business_timezone="America/Los_Angeles",
        notion_version="2022-06-28",
        version="0.01.00",
        cloud_run_execution="notion-visionlink-history-snapshot-abc",
    )
    values.update(overrides)
    return Config(**values)


def _run(pages, **config_overrides):
    fake = FakeNotion(source_database(), history_database())
    fake.pages[fake.source["id"]] = pages
    logs = []

    def logger(severity, message, **fields):
        logs.append((severity, message, fields))

    config = _config(**config_overrides)
    result = run_snapshot(config, fake, logger, now=EXECUTION, pause_seconds=0)
    return result, fake, logs


def test_property_mapping_snapshot_date_and_last_reported():
    result, fake, _logs = _run(
        [
            source_page(
                "57.29",
                hours=47451.616944,
                location="2498 W Base Line St, San Bernardino, CA 92410, USA",
                last_reported="2026-10-01T08:02:00.000Z",
            )
        ]
    )

    assert result.status == "SUCCESS"
    assert result.history_created == 1
    properties = fake.created[0][1]
    assert fake.created[0][0] == "0357c6bd-2650-4dfc-affb-72430beaca84"
    assert properties["Machine ID"]["title"][0]["text"]["content"] == "57.29"
    assert properties["Hours"]["number"] == 47451.616944
    assert "San Bernardino" in properties["Location"]["rich_text"][0]["text"]["content"]
    assert properties["Last Reported"]["date"]["start"] == "2026-10-01T08:02:00.000Z"
    snapshot = properties["Snapshot Date"]["date"]
    assert snapshot["start"] == "2026-10-01T10:05:11-07:00"
    assert snapshot["time_zone"] == "America/Los_Angeles"
    assert snapshot["start"] != properties["Last Reported"]["date"]["start"]
    assert properties["Snapshot Run ID"]["rich_text"][0]["text"]["content"] == (
        "2026-10-01T10:00:00_America-Los_Angeles"
    )
    assert properties["Status"]["select"]["name"] == "Asset Off"
    assert properties["Map"]["url"].endswith("query=34.12243,-117.34482")
    assert "relation" not in str(properties)
    assert "Machine" not in properties
    assert "Works Manager Project" not in properties
    assert "Assigned Contact" not in properties
    mapped_sources = {item.source_property for item in result.mappings}
    assert "Works Manager Project" not in mapped_sources
    assert "Assigned Contact" in result.unmapped_source_fields
    assert "Works Manager Project" in result.unmapped_source_fields


def test_missing_optional_fields_still_snapshots_machine():
    result, fake, _logs = _run([source_page("51.27", include_optional=False)])

    assert result.failed == 0
    assert result.history_created == 1
    properties = fake.created[0][1]
    assert "Last Reported" not in properties
    assert "Location" not in properties
    assert "Hours" not in properties
    assert properties["Snapshot Date"]["date"]["start"] == "2026-10-01T10:05:11-07:00"
    assert properties["Machine ID"]["title"][0]["text"]["content"] == "51.27"


def test_same_run_id_skips_and_later_run_id_inserts():
    page = source_page("57.29")
    first, fake, _logs = _run([page])
    assert first.history_created == 1

    logs = []
    second = run_snapshot(
        _config(),
        fake,
        lambda *args, **kwargs: logs.append((args, kwargs)),
        now=EXECUTION + timedelta(minutes=20),
        pause_seconds=0,
    )
    assert second.history_created == 0
    assert second.duplicates_skipped == 1
    assert second.failed == 0
    assert len(fake.created) == 1

    later = run_snapshot(
        _config(),
        fake,
        lambda *args, **kwargs: None,
        now=datetime(2026, 10, 2, 17, 2, tzinfo=timezone.utc),
        pause_seconds=0,
    )
    assert later.run_id == "2026-10-02T10:00:00_America-Los_Angeles"
    assert later.history_created == 1
    assert later.duplicates_skipped == 0
    assert len(fake.created) == 2
    assert later.run_id != first.run_id


def test_dry_run_reads_and_does_not_write():
    result, fake, logs = _run([source_page("57.29"), source_page("51.27")], dry_run=True)

    assert result.status == "SUCCESS"
    assert result.dry_run is True
    assert result.source_records == 2
    assert result.would_create == 2
    assert result.history_created == 0
    assert result.writes_performed == 0
    assert fake.write_calls == 0
    assert fake.created == []
    assert any(message == "dry run would create history record" for _sev, message, _fields in logs)


def test_one_failed_machine_does_not_stop_the_rest():
    fake = FakeNotion(source_database(), history_database(), fail_creates_for={"BAD"})
    fake.pages[fake.source["id"]] = [
        source_page("GOOD-1"),
        source_page("BAD"),
        source_page("GOOD-2"),
    ]
    logs = []
    result = run_snapshot(
        _config(),
        fake,
        lambda severity, message, **fields: logs.append((severity, message, fields)),
        now=EXECUTION,
        pause_seconds=0,
    )

    assert result.history_created == 2
    assert result.failed == 1
    assert result.status == "FAILURE"
    assert result.failed_machine_ids == ["BAD"]
    created_ids = [
        props["Machine ID"]["title"][0]["text"]["content"] for _db, props in fake.created
    ]
    assert created_ids == ["GOOD-1", "GOOD-2"]
    failure_logs = [
        (message, fields)
        for severity, message, fields in logs
        if message.startswith("machine snapshot failed")
    ]
    message, fields = failure_logs[0]
    assert fields["machineId"] == "BAD"
    assert fields["machineName"] == "BAD"
    assert fields["exceptionType"] == "NotionError"
    assert fields["notionStatus"] == 400
    assert "create failed" in fields["error"]
    assert "create failed" in fields["notionError"]
    assert "Machine ID" in fields["property"]
    assert fields["destinationDatabaseId"] == "0357c6bd-2650-4dfc-affb-72430beaca84"
    assert fields["runId"]
    assert "notionStatus=400" in message
    assert "exceptionType=NotionError" in message
    assert "secret_" not in fields["error"]
    assert "secret_" not in message


def test_incompatible_select_fails_that_machine_without_a_write():
    result, fake, logs = _run([source_page("57.29", status="Active"), source_page("51.27")])

    assert result.failed == 1
    assert result.history_created == 1
    assert result.status == "FAILURE"
    assert fake.created[0][1]["Machine ID"]["title"][0]["text"]["content"] == "51.27"
    assert any("Active" in problem for problem in result.schema_problems)
    failure = next(
        fields for _severity, message, fields in logs if message.startswith("machine snapshot failed")
    )
    assert failure["machineId"] == "57.29"
    assert failure["property"] == "Status"
    assert failure["notionStatus"] is None
    assert failure["exceptionType"] is None
    assert "Status" in failure["exceptionMessage"]
    assert "property=Status" in next(
        message for _severity, message, _fields in logs if message.startswith("machine snapshot failed")
    )


def test_run_id_slots_manual_runs_and_dst():
    from src.snapshot import SnapshotFatal

    january = datetime(2026, 1, 15, 18, 0, tzinfo=timezone.utc)  # 10:00 PST
    july = datetime(2026, 7, 15, 17, 0, tzinfo=timezone.utc)  # 10:00 PDT
    assert (
        resolve_run_id(january, slot="10:00", execution="exec", override=None)
        == "2026-01-15T10:00:00_America-Los_Angeles"
    )
    assert (
        resolve_run_id(july, slot="10:00", execution="exec", override=None)
        == "2026-07-15T10:00:00_America-Los_Angeles"
    )

    slot_time = datetime(2026, 10, 1, 17, 0, tzinfo=timezone.utc)  # 10:00 PDT
    slot_id = resolve_run_id(slot_time, slot="10:00", execution="exec", override=None)
    assert slot_id == "2026-10-01T10:00:00_America-Los_Angeles"
    before_slot = datetime(2026, 10, 1, 16, 30, tzinfo=timezone.utc)  # 09:30 PDT
    assert (
        resolve_run_id(before_slot, slot="10:00", execution="exec", override=None)
        == "2026-09-30T10:00:00_America-Los_Angeles"
    )

    # A retry hours later still anchors to the same 10:00 slot.
    later = slot_time + timedelta(hours=5)
    assert resolve_run_id(later, slot="10:00", execution="exec-2", override=None) == slot_id
    try:
        resolve_run_id(slot_time, slot="14:00", execution="exec", override=None)
    except SnapshotFatal:
        pass
    else:
        raise AssertionError("14:00 is not a scheduled slot")

    # Off-schedule manual runs do not use the 10:00 slot id, and retries share the execution name.
    manual_now = datetime(2026, 10, 1, 17, 5, tzinfo=timezone.utc)  # 10:05 PDT
    manual = resolve_run_id(manual_now, slot=None, execution="exec-manual", override=None)
    retried = resolve_run_id(
        manual_now + timedelta(hours=2),
        slot=None,
        execution="exec-manual",
        override=None,
    )
    later_manual = resolve_run_id(manual_now, slot=None, execution="exec-later", override=None)
    assert manual == "manual-exec-manual_America-Los_Angeles"
    assert retried == manual
    assert later_manual != manual
    assert manual != slot_id
    assert "10:00:00" not in manual
    assert "-07:00" not in manual
    assert "-08:00" not in slot_id
    assert "-07:00" not in slot_id


def test_schema_difference_log_names_expected_and_actual_types():
    history = history_database()
    history["properties"]["Location"]["type"] = "number"
    fake = FakeNotion(source_database(), history)
    fake.pages[fake.source["id"]] = [source_page("57.29")]
    logs = []
    run_snapshot(
        _config(),
        fake,
        lambda severity, message, **fields: logs.append((severity, message, fields)),
        now=EXECUTION,
        pause_seconds=0,
    )

    warnings = [
        (message, fields)
        for severity, message, fields in logs
        if severity == "WARNING" and message.startswith("schema differences")
    ]
    assert warnings
    message, fields = warnings[0]
    location = next(
        item for item in fields["incompatibleProperties"] if item["expectedProperty"] == "Location"
    )
    assert location["actualProperty"] == "Location"
    assert location["expectedType"] == "rich_text"
    assert location["actualType"] == "number"
    assert "Location" in message
    assert "expected rich_text" in message
    assert "actual Location number" in message
    assert "Latitude" in fields["missingProperties"]
    assert "Longitude" in fields["missingProperties"]
    assert fields["destinationDatabaseId"] == "0357c6bd-2650-4dfc-affb-72430beaca84"
