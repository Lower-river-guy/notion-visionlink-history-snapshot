"""Runtime configuration defaults."""

from src.config import MACHINES_DATABASE_ID, VERSION, load_config

MACHINES_DATA_SOURCE_ID = "241a2acd-f833-410a-9c0a-99e376add55e"
MACHINES_DATABASE = "8248e735-8458-4a00-9b41-cbe1eff6b975"


def test_machines_database_id_is_the_database_page_id():
    assert VERSION == "0.01.03"
    assert MACHINES_DATABASE_ID == MACHINES_DATABASE
    assert MACHINES_DATABASE_ID != MACHINES_DATA_SOURCE_ID

    config = load_config({"NOTION_TOKEN": "secret", "DRY_RUN": "true"})
    assert config.machines_database_id == MACHINES_DATABASE
    assert config.dry_run is True
