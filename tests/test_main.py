"""Process exit status."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from src.main import emit, run_job
from src.snapshot import format_summary
from tests.fakes import FakeNotion
from tests.notion_fixtures import history_database, source_database, source_page
from tests.test_snapshot import _config


def test_incomplete_run_returns_nonzero_and_summary_counts():
    fake = FakeNotion(source_database(), history_database(), fail_creates_for={"BAD"})
    fake.pages[fake.source["id"]] = [source_page("GOOD"), source_page("BAD")]

    code = run_job(
        _config(),
        fake,
        now=datetime(2026, 10, 1, 13, 5, tzinfo=timezone.utc),
        logger=lambda *args, **kwargs: None,
    )

    assert code == 1
    assert fake.write_calls == 2


def test_success_returns_zero_and_summary_shape():
    fake = FakeNotion(source_database(), history_database())
    fake.pages[fake.source["id"]] = [source_page("57.29")]
    captured = {}

    def logger(severity, message, **fields):
        captured[message] = fields

    code = run_job(
        _config(),
        fake,
        now=datetime(2026, 10, 1, 17, 0, tzinfo=timezone.utc),
        logger=logger,
    )

    assert code == 0
    finished = captured["snapshot finished"]
    assert finished["version"] == "0.01.00"
    assert finished["runId"] == "2026-10-01T10:00:00_America-Los_Angeles"
    assert finished["sourceRecords"] == 1
    assert finished["historyCreated"] == 1
    assert finished["duplicatesSkipped"] == 0
    assert finished["recordsFailed"] == 0
    assert finished["status"] == "SUCCESS"
    assert finished["durationSeconds"] >= 0

    from src.snapshot import run_snapshot

    result = run_snapshot(
        _config(dry_run=True),
        fake,
        lambda *args, **kwargs: None,
        now=datetime(2026, 10, 2, 17, 0, tzinfo=timezone.utc),
        pause_seconds=0,
    )
    assert result.would_create == 1
    assert result.history_created == 0
    text = format_summary(result)
    assert "Version: 0.01.00" in text
    assert "Status: SUCCESS" in text
    assert "Writes performed: 0" in text


def test_emit_redacts_token_like_authorization_header(capsys):
    token = "ntn_supersecrettokenvalue"
    header = f"Authorization: Bearer {token}"
    emit("ERROR", header, notionError=header, authorization=header)
    captured = capsys.readouterr().out
    assert token not in captured
    assert "Bearer [redacted]" in captured
    record = json.loads(captured)
    assert "authorization" not in record
    assert token not in record["notionError"]
