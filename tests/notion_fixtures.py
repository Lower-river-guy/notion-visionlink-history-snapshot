"""Database fixtures shaped like the live VisionLink schemas."""

from __future__ import annotations


def _select(options):
    return {"type": "select", "select": {"options": [{"name": name} for name in options]}}


def source_database():
    return {
        "id": "3db284de-cb43-80ed-9b6f-fc20d6cc20eb",
        "title": [{"plain_text": "Cat VisionLink"}],
        "properties": {
            "Machine ID": {"type": "title", "title": {}},
            "Hours": {"type": "number", "number": {}},
            "Location": {"type": "rich_text", "rich_text": {}},
            "Last Reported": {"type": "date", "date": {}},
            "Fuel %": {"type": "number", "number": {}},
            "Job Number": {"type": "rich_text", "rich_text": {}},
            "Make": {"type": "rich_text", "rich_text": {}},
            "Map": {"type": "url", "url": {}},
            "Model": {"type": "rich_text", "rich_text": {}},
            "Serial Number": {"type": "rich_text", "rich_text": {}},
            "Status": _select(
                [
                    "Active",
                    "Needs Service",
                    "Out of Service",
                    "Spare",
                    "Retired",
                    "Asset On",
                    "Asset Off",
                    "No Status Reported",
                    "Not Supported",
                ]
            ),
            "Equipment Type": _select(
                [
                    "Machine",
                    "Tablet",
                    "Data Collector",
                    "Rover",
                    "Base",
                    "Machine Control",
                    "Modem",
                    "Radio",
                    "Other",
                ]
            ),
            "Assigned Contact": {"type": "relation", "relation": {}},
            "Works Manager Project": {"type": "relation", "relation": {}},
        },
    }


def history_database():
    return {
        "id": "0357c6bd-2650-4dfc-affb-72430beaca84",
        "title": [{"plain_text": "Cat VisionLink History"}],
        "properties": {
            "Machine ID": {"type": "title", "title": {}},
            "Hours": {"type": "number", "number": {}},
            "Location": {"type": "rich_text", "rich_text": {}},
            "Last Reported": {"type": "date", "date": {}},
            "Fuel %": {"type": "number", "number": {}},
            "Job Number": {"type": "rich_text", "rich_text": {}},
            "Make": {"type": "rich_text", "rich_text": {}},
            "Map": {"type": "url", "url": {}},
            "Model": {"type": "rich_text", "rich_text": {}},
            "Serial Number": {"type": "rich_text", "rich_text": {}},
            "Status": _select(["Asset On", "Asset Off", "No Status Reported", "Not Supported"]),
            "Equipment Type": _select(["Dozer", "Scraper", "Excavator"]),
            "Snapshot Date": {"type": "date", "date": {}},
            "Snapshot Run ID": {"type": "rich_text", "rich_text": {}},
            "Machine": {
                "type": "relation",
                "relation": {"database_id": "8248e735-8458-4a00-9b41-cbe1eff6b975"},
            },
            "Machine Series": {"type": "formula", "formula": {}},
            "Related to Projects (VisionLink History)": {"type": "relation", "relation": {}},
        },
    }


def machines_database():
    return {
        "id": "8248e735-8458-4a00-9b41-cbe1eff6b975",
        "title": [{"plain_text": "Machines"}],
        "properties": {
            "Machine ID": {"type": "title", "title": {}},
            "VisionLink History": {"type": "relation", "relation": {}},
        },
    }


def machine_page(machine_id, page_id=None):
    return {
        "id": page_id or f"machines-page-{machine_id}",
        "properties": {
            "Machine ID": {"type": "title", "title": [{"plain_text": machine_id}]},
        },
    }


def seed_machines(fake, pages):
    """Give each source page one Machines record with the same Machine ID."""

    ids = []
    for page in pages:
        title = page["properties"]["Machine ID"]["title"]
        if title:
            ids.append(title[0]["plain_text"])
    fake.pages[fake.machines["id"]] = [machine_page(machine_id) for machine_id in dict.fromkeys(ids)]


def source_page(
    machine_id,
    *,
    hours=10.5,
    location="Yard",
    last_reported="2026-09-30T08:02:00.000Z",
    last_reported_time_zone=None,
    map_url="https://www.google.com/maps/search/?api=1&query=34.12243,-117.34482",
    status="Asset Off",
    equipment_type=None,
    include_optional=True,
):
    properties = {
        "Machine ID": {"type": "title", "title": [{"plain_text": machine_id}]},
        "Assigned Contact": {"type": "relation", "relation": []},
        "Works Manager Project": {"type": "relation", "relation": []},
    }
    if include_optional:
        properties.update(
            {
                "Hours": {"type": "number", "number": hours},
                "Location": {"type": "rich_text", "rich_text": [{"plain_text": location}]},
                "Fuel %": {"type": "number", "number": None},
                "Job Number": {"type": "rich_text", "rich_text": [{"plain_text": "3080H"}]},
                "Make": {"type": "rich_text", "rich_text": [{"plain_text": "CAT"}]},
                "Map": {"type": "url", "url": map_url},
                "Model": {"type": "rich_text", "rich_text": [{"plain_text": "657E"}]},
                "Serial Number": {"type": "rich_text", "rich_text": [{"plain_text": "91Z00301"}]},
                "Status": {
                    "type": "select",
                    "select": {"name": status} if status else None,
                },
                "Equipment Type": {
                    "type": "select",
                    "select": {"name": equipment_type} if equipment_type else None,
                },
            }
        )
        if last_reported is not None:
            properties["Last Reported"] = {
                "type": "date",
                "date": {
                    "start": last_reported,
                    "end": None,
                    "time_zone": last_reported_time_zone,
                },
            }
    return {"id": f"src-{machine_id}", "properties": properties}
