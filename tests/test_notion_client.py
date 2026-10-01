"""Notion client pagination, retries, and write guards."""

from __future__ import annotations

import pytest

from src.notion_client import NotionClient, NotionError


class FakeResponse:
    def __init__(self, status, payload=None, headers=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = text or ""

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, json=None, headers=None, timeout=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "json": json,
                "timeout": timeout,
                "authorization": (headers or {}).get("Authorization"),
            }
        )
        if not self.responses:
            raise AssertionError("unexpected request")
        return self.responses.pop(0)


def _client(responses, sleeper=None):
    sleeps = []

    def _sleep(seconds):
        sleeps.append(seconds)

    session = FakeSession(responses)
    client = NotionClient(
        "secret_testtokenvalue",
        session=session,
        sleeper=sleeper or _sleep,
        max_attempts=4,
    )
    return client, session, sleeps


def test_source_query_follows_pagination_cursor():
    page_one = {
        "results": [{"id": "page-1", "properties": {}}],
        "has_more": True,
        "next_cursor": "cursor-2",
    }
    page_two = {
        "results": [{"id": "page-2", "properties": {}}],
        "has_more": False,
        "next_cursor": None,
    }
    client, session, _sleeps = _client(
        [FakeResponse(200, page_one), FakeResponse(200, page_two)]
    )

    pages = client.query_database("3db284decb4380ed9b6ffc20d6cc20eb")

    assert [page["id"] for page in pages] == ["page-1", "page-2"]
    assert session.calls[0]["json"]["page_size"] == 100
    assert "start_cursor" not in session.calls[0]["json"]
    assert session.calls[1]["json"]["start_cursor"] == "cursor-2"
    assert session.calls[0]["url"].endswith(
        "/v1/databases/3db284de-cb43-80ed-9b6f-fc20d6cc20eb/query"
    )


def test_notion_429_retries_then_succeeds():
    client, session, sleeps = _client(
        [
            FakeResponse(429, {"message": "rate limited"}, headers={"Retry-After": "0"}),
            FakeResponse(200, {"id": "db", "title": [], "properties": {}}),
        ]
    )

    payload = client.retrieve_database("3db284de-cb43-80ed-9b6f-fc20d6cc20eb")

    assert payload["id"] == "db"
    assert len(session.calls) == 2
    assert sleeps == [0.0]


def test_retryable_5xx_and_timeout_use_backoff():
    class TimeoutThenOk:
        def __init__(self):
            self.calls = 0

        def request(self, method, url, json=None, headers=None, timeout=None):
            self.calls += 1
            if self.calls == 1:
                import requests

                raise requests.Timeout("timed out")
            return FakeResponse(200, {"ok": True})

    sleeps = []
    session = TimeoutThenOk()
    client = NotionClient("secret_testtokenvalue", session=session, sleeper=sleeps.append, max_attempts=3)
    assert client.retrieve_database("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")["ok"] is True
    assert sleeps and sleeps[0] > 0

    client, session, sleeps = _client(
        [
            FakeResponse(503, {"message": "unavailable"}),
            FakeResponse(200, {"ok": True}),
        ]
    )
    assert client.retrieve_database("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")["ok"] is True
    assert len(session.calls) == 2
    assert sleeps[0] == 0.5


def test_error_text_redacts_token():
    client, _session, _sleeps = _client(
        [FakeResponse(400, {"message": "bad secret_testtokenvalue"}, text="secret_testtokenvalue")]
    )
    with pytest.raises(NotionError) as caught:
        client.retrieve_database("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    assert "secret_testtokenvalue" not in str(caught.value)
    assert "[redacted]" in str(caught.value)


def test_create_refuses_source_database_and_relations():
    client, session, _sleeps = _client([])
    source = "3db284de-cb43-80ed-9b6f-fc20d6cc20eb"
    with pytest.raises(NotionError, match="protected"):
        client.create_page(source, {"Machine ID": {"title": []}}, forbidden_database_ids=(source,))
    with pytest.raises(NotionError, match="relation"):
        client.create_page(
            "0357c6bd-2650-4dfc-affb-72430beaca84",
            {"Machine": {"relation": [{"id": "x"}]}},
            forbidden_database_ids=(source,),
        )
    assert session.calls == []
