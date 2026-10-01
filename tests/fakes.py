"""In-memory Notion stand-in for snapshot tests."""

from __future__ import annotations

from src.notion_client import NotionError, database_title


class FakeNotion:
    def __init__(self, source, destination, *, fail_creates_for=()):
        self.source = source
        self.destination = destination
        self.pages = {source["id"]: [], destination["id"]: []}
        self.fail_creates_for = set(fail_creates_for)
        self.created = []
        self.write_calls = 0

    def retrieve_database(self, database_id):
        if database_id == self.source["id"]:
            return self.source
        if database_id == self.destination["id"]:
            return self.destination
        raise NotionError(f"Cannot access database {database_id}", status_code=404, database_id=database_id)

    def search_databases_by_title(self, title):
        if database_title(self.destination) == title:
            return [self.destination]
        return []

    def query_database(self, database_id, filter_body=None):
        rows = list(self.pages[database_id])
        if not filter_body:
            return rows
        expected = filter_body["rich_text"]["equals"]
        matched = []
        for page in rows:
            run_id = _rich_text_value(page, "Snapshot Run ID")
            if run_id == expected:
                matched.append(page)
        return matched

    def create_page(self, database_id, properties, forbidden_database_ids=()):
        self.write_calls += 1
        if database_id in forbidden_database_ids:
            raise NotionError("Refusing to write: target database is protected", database_id=database_id)
        machine_id = _title_value(properties)
        if machine_id in self.fail_creates_for:
            raise NotionError(f"create failed for {machine_id}", status_code=400, database_id=database_id)
        page = {
            "id": f"created-{machine_id}-{self.write_calls}",
            "properties": _as_read_properties(properties),
        }
        self.pages[database_id].append(page)
        self.created.append((database_id, properties))
        return page


def _title_value(properties):
    chunks = properties["Machine ID"]["title"]
    return "".join(chunk["text"]["content"] for chunk in chunks)


def _rich_text_value(page, name):
    prop = page["properties"][name]
    chunks = prop.get("rich_text") or []
    if not chunks:
        return None
    chunk = chunks[0]
    if chunk.get("plain_text"):
        return chunk["plain_text"]
    return chunk["text"]["content"]


def _as_read_properties(properties):
    """Store creates the way Notion returns them from a query."""

    read = {}
    for name, value in properties.items():
        if "title" in value:
            text = value["title"][0]["text"]["content"]
            read[name] = {"type": "title", "title": [{"plain_text": text}]}
        elif "rich_text" in value:
            text = value["rich_text"][0]["text"]["content"]
            read[name] = {"type": "rich_text", "rich_text": [{"plain_text": text}]}
        elif "number" in value:
            read[name] = {"type": "number", "number": value["number"]}
        elif "url" in value:
            read[name] = {"type": "url", "url": value["url"]}
        elif "select" in value:
            read[name] = {"type": "select", "select": {"name": value["select"]["name"]}}
        elif "date" in value:
            read[name] = {"type": "date", "date": dict(value["date"])}
        else:
            read[name] = value
    return read
