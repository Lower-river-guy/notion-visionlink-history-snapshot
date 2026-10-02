"""Period boundaries, meter deltas, and Machines number writes.

Edge cases: Monday week and calendar month in America/Los_Angeles, including
the March and November daylight-saving boundaries; a reading exactly on
Monday 00:00; one reading; no reading before the period; no reading inside
the period; a normal later-minus-earlier delta; a zero delta; half-up
rounding to 0.1; duplicate timestamps with the same hours; duplicate
timestamps or snapshot run ids whose hours disagree; unsorted input; a meter
decrease; a negative result; a reading after now; a date-only snapshot; and
dry-run skipping the Machines write.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.main import run_job
from src.utilization import (
    UtilizationFatal,
    Reading,
    calculate_machine_utilization,
    calculate_period_hours,
    get_period_boundaries,
    normalize_history,
    run_utilization,
    update_machine_utilization,
)
from tests.fakes import FakeNotion
from tests.notion_fixtures import history_database, seed_machines, source_database, source_page
from tests.test_snapshot import _config

LA = ZoneInfo("America/Los_Angeles")


def _at(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=LA)


def _reading(when, hours, run_id=None, page_id=None):
    return Reading(timestamp=when, hours=hours, snapshot_run_id=run_id, page_id=page_id)


def test_period_boundaries_use_monday_and_calendar_month_in_los_angeles():
    # Friday 2026-10-02 01:37 PDT.
    boundaries = get_period_boundaries(datetime(2026, 10, 2, 8, 37, tzinfo=ZoneInfo("UTC")))

    assert boundaries.this_week.start == _at(2026, 9, 28)
    assert boundaries.this_week.end == _at(2026, 10, 5)
    assert boundaries.last_week.start == _at(2026, 9, 21)
    assert boundaries.last_week.end == _at(2026, 9, 28)
    assert boundaries.this_month.start == _at(2026, 10, 1)
    assert boundaries.this_month.end == _at(2026, 11, 1)
    assert boundaries.this_week.start.utcoffset().total_seconds() == -7 * 3600
    assert boundaries.this_month.end.utcoffset().total_seconds() == -7 * 3600

    # Monday after the March 2026 daylight-saving change is PDT.
    spring = get_period_boundaries(_at(2026, 3, 9, 10))
    assert spring.this_week.start == _at(2026, 3, 9)
    assert spring.this_week.start.utcoffset().total_seconds() == -7 * 3600
    assert spring.last_week.start == _at(2026, 3, 2)
    assert spring.last_week.start.utcoffset().total_seconds() == -8 * 3600

    # Monday after the November 2026 change is PST.
    fall = get_period_boundaries(_at(2026, 11, 2, 10))
    assert fall.this_week.start == _at(2026, 11, 2)
    assert fall.this_week.start.utcoffset().total_seconds() == -8 * 3600
    assert fall.this_month.start == _at(2026, 11, 1)
    assert fall.this_month.start.utcoffset().total_seconds() == -7 * 3600
    assert fall.this_month.end == _at(2026, 12, 1)
    assert fall.this_month.end.utcoffset().total_seconds() == -8 * 3600


def test_monday_midnight_starts_the_new_week():
    now = _at(2026, 10, 2, 10)
    readings = [
        _reading(_at(2026, 9, 27, 10), 100),  # last week
        _reading(_at(2026, 9, 20, 10), 90),  # before last week
        _reading(_at(2026, 9, 28, 0), 110),  # Monday 00:00, this week
    ]
    result = calculate_machine_utilization("90.35", readings, now)
    assert result.hours_last_week == 10.0
    assert result.hours_this_week == 10.0


def test_blank_when_one_reading_or_period_is_not_bracketed():
    now = _at(2026, 10, 2, 10)
    only = calculate_machine_utilization("90.35", [_reading(_at(2026, 10, 1, 10), 50)], now)
    assert only.hours_this_week is None
    assert only.hours_last_week is None
    assert only.hours_this_month is None

    inside_only = calculate_machine_utilization(
        "90.35",
        [_reading(_at(2026, 9, 29, 10), 40), _reading(_at(2026, 10, 1, 10), 48)],
        now,
    )
    # Both are inside this week, and neither is before Monday Sep 28.
    assert inside_only.hours_this_week is None
    assert inside_only.hours_this_month == 8.0

    no_reading_this_month = calculate_machine_utilization(
        "90.35",
        [_reading(_at(2026, 9, 20, 10), 10), _reading(_at(2026, 9, 24, 10), 18)],
        now,
    )
    assert no_reading_this_month.hours_last_week == 8.0
    assert no_reading_this_month.hours_this_week is None
    assert no_reading_this_month.hours_this_month is None


def test_delta_is_later_minus_earlier_and_rounds_half_up():
    now = _at(2026, 10, 2, 10)
    readings = [
        _reading(_at(2026, 9, 24, 10), 100),
        _reading(_at(2026, 9, 30, 10), 110),
        _reading(_at(2026, 10, 1, 10), 110.25),
    ]
    result = calculate_machine_utilization("90.35", readings, now)
    # This week uses the Sep 24 meter, the last reading before Monday.
    assert result.hours_this_week == 10.3
    assert result.hours_this_month == 0.3
    assert result.hours_last_week is None  # Sep 24 is inside last week, but nothing is before Sep 21

    half_up = calculate_machine_utilization(
        "90.35",
        [
            _reading(_at(2026, 9, 27, 10), 100),
            _reading(_at(2026, 9, 20, 10), 90),
            _reading(_at(2026, 10, 1, 10), 110.25),
        ],
        now,
    )
    assert half_up.hours_this_week == 10.3
    assert half_up.hours_last_week == 10.0


def test_zero_delta_is_a_value_and_future_readings_are_ignored():
    now = _at(2026, 10, 2, 10)
    result = calculate_machine_utilization(
        "90.35",
        [
            _reading(_at(2026, 9, 27, 10), 40),
            _reading(_at(2026, 9, 20, 10), 30),
            _reading(_at(2026, 10, 1, 10), 40),
            _reading(_at(2026, 10, 3, 10), 80),
        ],
        now,
    )
    assert result.hours_this_week == 0.0
    assert result.hours_last_week == 10.0


def test_duplicates_resets_and_unsorted_rows():
    now = _at(2026, 10, 2, 10)
    baseline = _reading(_at(2026, 9, 20, 10), 100, page_id="a")
    last_week = _reading(_at(2026, 9, 24, 10), 110, page_id="b")
    duplicate = _reading(_at(2026, 9, 24, 10), 110, run_id="run-1", page_id="c")
    same_run = _reading(_at(2026, 9, 24, 10, 5), 110, run_id="run-1", page_id="d")
    this_week = _reading(_at(2026, 10, 1, 10), 125.2, page_id="e")
    ordered = [this_week, same_run, baseline, duplicate, last_week]
    result = calculate_machine_utilization("57.22", ordered, now)
    assert result.hours_last_week == 10.0
    assert result.hours_this_week == 15.2

    conflict = calculate_machine_utilization(
        "57.22",
        [
            baseline,
            _reading(_at(2026, 9, 24, 10), 110, run_id="run-1"),
            _reading(_at(2026, 9, 24, 10), 400, run_id="run-1"),
            this_week,
        ],
        now,
    )
    assert conflict.hours_last_week is None
    assert conflict.hours_this_week is None

    reset = calculate_machine_utilization(
        "57.22",
        [baseline, last_week, _reading(_at(2026, 9, 30, 10), 20), this_week],
        now,
    )
    assert reset.hours_this_week is None
    assert reset.hours_last_week == 10.0

    history = normalize_history(list(reversed(ordered)), now=now)
    assert history.out_of_order is True
    assert calculate_period_hours(
        history,
        get_period_boundaries(now).this_week.start,
        get_period_boundaries(now).this_week.end,
    ) == 15.2


def test_history_page_dates_and_dry_run_does_not_write():
    from src.utilization import reading_from_history_page

    page = {
        "id": "hist-1",
        "properties": {
            "Machine ID": {"type": "title", "title": [{"plain_text": "90.35"}]},
            "Hours": {"type": "number", "number": 12.5},
            "Snapshot Date": {
                "type": "date",
                "date": {"start": "2026-10-01T10:00:00", "time_zone": "America/Los_Angeles"},
            },
            "Snapshot Run ID": {"type": "rich_text", "rich_text": [{"plain_text": "run"}]},
        },
    }
    machine_id, reading = reading_from_history_page(page, "America/Los_Angeles")
    assert machine_id == "90.35"
    assert reading.timestamp == _at(2026, 10, 1, 10)
    assert reading.snapshot_run_id == "run"

    date_only = {
        "id": "hist-2",
        "properties": {
            "Machine ID": {"type": "title", "title": [{"plain_text": "90.35"}]},
            "Hours": {"type": "number", "number": 1},
            "Snapshot Date": {"type": "date", "date": {"start": "2026-09-24"}},
        },
    }
    _, dated = reading_from_history_page(date_only, "America/Los_Angeles")
    assert dated.timestamp == _at(2026, 9, 24)

    calls = []

    class Exploding:
        def update_machine_numbers(self, page_id, properties):
            calls.append(page_id)
            raise AssertionError("dry run must not update Machines")

    wrote = update_machine_utilization(
        Exploding(),
        "page-1",
        calculate_machine_utilization("90.35", [_reading(_at(2026, 10, 1, 10), 5)], _at(2026, 10, 2, 10)),
        dry_run=True,
        logger=lambda *args, **kwargs: None,
    )
    assert wrote is False
    assert calls == []


def test_run_utilization_writes_only_three_numbers_and_dry_run_skips_them():
    now = _at(2026, 10, 2, 10)
    fake = FakeNotion(source_database(), history_database())
    fake.pages[fake.machines["id"]] = [
        {
            "id": "machines-page-90.35",
            "properties": {
                "Machine ID": {"type": "title", "title": [{"plain_text": "90.35"}]},
            },
        }
    ]
    fake.pages[fake.destination["id"]] = [
        _history("90.35", "2026-09-24", 100),
        _history("90.35", "2026-09-30T10:00:00", 110, time_zone="America/Los_Angeles"),
        _history("90.35", "2026-10-01T10:00:00", 118.25, time_zone="America/Los_Angeles"),
    ]
    logs = []
    result = run_utilization(
        _config(dry_run=False),
        fake,
        lambda severity, message, **fields: logs.append((message, fields)),
        now=now,
        destination_database_id=fake.destination["id"],
        pause_seconds=0,
    )
    assert result.updated == 1
    assert result.failed == 0
    page_id, properties = fake.machine_updates[0]
    assert page_id == "machines-page-90.35"
    assert set(properties) == {"Hours This Week", "Hours Last Week", "Hours This Month"}
    assert properties["Hours This Week"] == {"number": 18.3}
    assert properties["Hours Last Week"] == {"number": None}
    assert properties["Hours This Month"] == {"number": 8.3}
    assert any(message == "utilization finished" for message, _fields in logs)

    fake.machine_updates.clear()
    dry = run_utilization(
        _config(dry_run=True),
        fake,
        lambda *args, **kwargs: None,
        now=now,
        destination_database_id=fake.destination["id"],
        pause_seconds=0,
    )
    assert dry.dry_run is True
    assert dry.updated == 0
    assert fake.machine_updates == []
    assert dry.sample[0]["machineId"] == "90.35"
    assert dry.sample[0]["hoursThisWeek"] == 18.3


def test_job_dry_run_does_not_update_machines():
    pages = [source_page("90.35", hours=20)]
    fake = FakeNotion(source_database(), history_database())
    fake.pages[fake.source["id"]] = pages
    seed_machines(fake, pages)
    code = run_job(
        _config(dry_run=True),
        fake,
        now=_at(2026, 10, 2, 10),
        logger=lambda *args, **kwargs: None,
    )
    assert code == 0
    assert fake.write_calls == 0
    assert fake.machine_updates == []


def test_missing_number_property_fails_before_a_machine_write():
    fake = FakeNotion(source_database(), history_database())
    del fake.machines["properties"]["Hours This Week"]
    with pytest.raises(UtilizationFatal, match="missing number"):
        run_utilization(
            _config(),
            fake,
            lambda *args, **kwargs: None,
            now=_at(2026, 10, 2, 10),
            destination_database_id=fake.destination["id"],
            pause_seconds=0,
        )
    assert fake.machine_updates == []


def _history(machine_id, start, hours, time_zone=None):
    return {
        "id": f"hist-{machine_id}-{start}",
        "properties": {
            "Machine ID": {"type": "title", "title": [{"plain_text": machine_id}]},
            "Hours": {"type": "number", "number": hours},
            "Snapshot Date": {
                "type": "date",
                "date": {"start": start, "time_zone": time_zone},
            },
            "Snapshot Run ID": {
                "type": "rich_text",
                "rich_text": [{"plain_text": f"run-{start}"}],
            },
        },
    }
