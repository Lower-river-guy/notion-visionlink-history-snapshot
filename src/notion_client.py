"""Notion access for the history snapshot and Machines hour deltas.

Page creates are append-only in Cat VisionLink History. The only relation
write is the history property "Machine". The only page update is the three
Machines number properties Hours This Week, Hours Last Week, and Hours This
Month. There is no archive or delete method. The source database id is
refused as a write parent.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable

import requests

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_SECRET_RE = re.compile(r"(?i)(bearer\s+)\S+|((?:secret_|ntn_)[A-Za-z0-9_\-]+)")
_MAX_PAGES = 10_000
HISTORY_DATABASE_ID = "0357c6bd-2650-4dfc-affb-72430beaca84"
ALLOWED_RELATION_PROPERTY = "Machine"
MACHINE_NUMBER_PROPERTIES = (
    "Hours This Week",
    "Hours Last Week",
    "Hours This Month",
)


class NotionError(Exception):
    """A Notion call failed. The message is safe to log."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        database_id: str | None = None,
        response_body: str | None = None,
    ) -> None:
        super().__init__(redact_secrets(message))
        self.status_code = status_code
        self.database_id = database_id
        self.response_body = redact_secrets(response_body) if response_body else None


def redact_secrets(text: str) -> str:
    """Remove token-like substrings from a log or error string."""

    def _replace(match: re.Match[str]) -> str:
        if match.group(1):
            return match.group(1) + "[redacted]"
        return "[redacted]"

    return _SECRET_RE.sub(_replace, text)


def normalize_notion_id(value: str) -> str:
    raw = value.replace("-", "").strip().lower()
    if len(raw) != 32 or any(char not in "0123456789abcdef" for char in raw):
        return value.strip()
    return f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"


def same_notion_id(left: str, right: str) -> bool:
    return normalize_notion_id(left).replace("-", "") == normalize_notion_id(right).replace("-", "")


def _require_allowed_relation(database_id: str, name: str, value: dict[str, Any]) -> None:
    """Allow only History.Machine, with exactly one related page id."""

    if name != ALLOWED_RELATION_PROPERTY or not same_notion_id(database_id, HISTORY_DATABASE_ID):
        raise NotionError(
            f"Refusing to write relation property {name!r}",
            database_id=database_id,
        )
    if set(value) != {"relation"}:
        raise NotionError(
            "Refusing to write relation property 'Machine' with extra fields",
            database_id=database_id,
        )
    relation = value["relation"]
    if (
        not isinstance(relation, list)
        or len(relation) != 1
        or not isinstance(relation[0], dict)
        or set(relation[0]) != {"id"}
        or not str(relation[0].get("id") or "").strip()
    ):
        raise NotionError(
            "Machine relation must contain exactly one page id",
            database_id=database_id,
        )


class NotionClient:
    """Small Notion REST client with bounded exponential backoff."""

    def __init__(
        self,
        token: str,
        *,
        notion_version: str = "2022-06-28",
        base_url: str = "https://api.notion.com",
        session: Any | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        max_attempts: int = 6,
        timeout: float = 30.0,
    ) -> None:
        if not token or not token.strip():
            raise NotionError("NOTION_TOKEN is empty")
        self._token = token.strip()
        self.base_url = base_url.rstrip("/")
        self.notion_version = notion_version
        self.session = session or requests.Session()
        self.sleeper = sleeper
        self.max_attempts = max_attempts
        self.timeout = timeout

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": self.notion_version,
            "Content-Type": "application/json",
        }

    def retrieve_database(self, database_id: str) -> dict[str, Any]:
        database_id = normalize_notion_id(database_id)
        return self._request(
            "GET",
            f"/v1/databases/{database_id}",
            database_id=database_id,
        )

    def search_databases_by_title(self, title: str) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        pages_read = 0
        while True:
            body: dict[str, Any] = {
                "query": title,
                "filter": {"value": "database", "property": "object"},
                "page_size": 100,
            }
            if cursor:
                body["start_cursor"] = cursor
            data = self._request("POST", "/v1/search", body)
            pages_read += 1
            for item in data.get("results") or []:
                if item.get("object") != "database":
                    continue
                if database_title(item) == title:
                    matches.append(item)
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            if not cursor:
                break
            if cursor in seen_cursors or pages_read > 100:
                raise NotionError("Notion search pagination did not advance")
            seen_cursors.add(cursor)
        return matches

    def query_database(
        self,
        database_id: str,
        *,
        filter_body: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Read every page, following next_cursor until has_more is false."""

        database_id = normalize_notion_id(database_id)
        pages: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            body: dict[str, Any] = {"page_size": 100}
            if filter_body:
                body["filter"] = filter_body
            if cursor:
                if cursor in seen_cursors:
                    raise NotionError(
                        "Notion query returned a repeated pagination cursor",
                        database_id=database_id,
                    )
                seen_cursors.add(cursor)
                body["start_cursor"] = cursor
            data = self._request(
                "POST",
                f"/v1/databases/{database_id}/query",
                body,
                database_id=database_id,
            )
            batch = data.get("results") or []
            pages.extend(batch)
            if len(pages) > _MAX_PAGES:
                raise NotionError(
                    "Notion query exceeded the pagination safety limit",
                    database_id=database_id,
                )
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            if not cursor:
                raise NotionError(
                    "Notion query set has_more without next_cursor",
                    database_id=database_id,
                )
        return pages

    def create_page(
        self,
        database_id: str,
        properties: dict[str, Any],
        *,
        forbidden_database_ids: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """Append one page. Refuses protected databases and unlisted relations."""

        database_id = normalize_notion_id(database_id)
        for forbidden in forbidden_database_ids:
            if same_notion_id(database_id, forbidden):
                raise NotionError(
                    "Refusing to write: target database is protected",
                    database_id=database_id,
                )
        for name, value in properties.items():
            if isinstance(value, dict) and "relation" in value:
                _require_allowed_relation(database_id, name, value)
        return self._request(
            "POST",
            "/v1/pages",
            {"parent": {"database_id": database_id}, "properties": properties},
            database_id=database_id,
        )

    def update_machine_numbers(self, page_id: str, properties: dict[str, Any]) -> dict[str, Any]:
        """Patch one Machines page. Only the three utilization numbers are allowed."""

        page_id = normalize_notion_id(page_id)
        if not page_id:
            raise NotionError("Refusing to update a Machines page without an id")
        if set(properties) != set(MACHINE_NUMBER_PROPERTIES):
            raise NotionError(
                "Machines utilization update must set only "
                + ", ".join(MACHINE_NUMBER_PROPERTIES)
            )
        payload: dict[str, Any] = {}
        for name in MACHINE_NUMBER_PROPERTIES:
            value = properties[name]
            if not isinstance(value, dict) or set(value) != {"number"}:
                raise NotionError(f"Refusing to write {name!r} with a non-number payload")
            number = value["number"]
            if number is not None and (
                isinstance(number, bool) or not isinstance(number, (int, float))
            ):
                raise NotionError(f"Refusing to write {name!r}: value is not a number")
            if isinstance(number, float) and (
                number != number or number in (float("inf"), float("-inf"))
            ):
                raise NotionError(f"Refusing to write {name!r}: value is not finite")
            payload[name] = {"number": None if number is None else float(number)}
        return self._request(
            "PATCH",
            f"/v1/pages/{page_id}",
            {"properties": payload},
        )

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        database_id: str | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        last_error: NotionError | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    json=body,
                    headers=self._headers,
                    timeout=self.timeout,
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = NotionError(
                    f"Network error talking to Notion ({type(exc).__name__})",
                    database_id=database_id,
                )
                if attempt == self.max_attempts:
                    raise last_error
                self.sleeper(self._delay(attempt, None))
                continue

            status = response.status_code
            if status in RETRYABLE_STATUS:
                body = _response_message(response)
                last_error = NotionError(
                    f"Notion HTTP {status}: {body}",
                    status_code=status,
                    database_id=database_id,
                    response_body=body,
                )
                if attempt == self.max_attempts:
                    raise last_error
                retry_after = response.headers.get("Retry-After") if response.headers else None
                self.sleeper(self._delay(attempt, retry_after))
                continue
            if status >= 400:
                body = _response_message(response)
                raise NotionError(
                    f"Notion HTTP {status}: {body}",
                    status_code=status,
                    database_id=database_id,
                    response_body=body,
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise NotionError(
                    "Notion returned a non-JSON response",
                    status_code=status,
                    database_id=database_id,
                ) from exc
            if not isinstance(payload, dict):
                raise NotionError(
                    "Notion returned an unexpected response",
                    status_code=status,
                    database_id=database_id,
                )
            return payload
        raise last_error or NotionError("Notion request failed", database_id=database_id)

    @staticmethod
    def _delay(attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                parsed = float(retry_after)
            except ValueError:
                parsed = -1
            if parsed >= 0:
                return min(parsed, 60.0)
        return min(30.0, 0.5 * (2 ** (attempt - 1)))


def database_title(database: dict[str, Any]) -> str:
    parts = database.get("title") or []
    if isinstance(parts, str):
        return parts.strip()
    return "".join(part.get("plain_text", "") for part in parts if isinstance(part, dict)).strip()


def _response_message(response: Any) -> str:
    text = ""
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("code") or ""
        text = str(message)
    if not text:
        text = getattr(response, "text", "") or ""
    text = redact_secrets(str(text)).replace("\n", " ").strip()
    return text[:500] or "no error body"
