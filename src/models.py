"""Data shapes for a VisionLink history snapshot."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class FieldMapping:
    """One source property copied onto a history property."""

    source_property: str
    source_type: str
    history_property: str
    history_type: str


@dataclass
class SchemaPlan:
    """How this run will copy source properties onto the history database."""

    source_database_id: str
    source_database_title: str
    destination_database_id: str
    destination_database_title: str
    mappings: list[FieldMapping] = field(default_factory=list)
    unmapped_source_fields: list[str] = field(default_factory=list)
    untouched_destination_fields: list[str] = field(default_factory=list)
    schema_problems: list[str] = field(default_factory=list)
    schema_differences: list[dict] = field(default_factory=list)
    select_options: dict[str, set[str]] = field(default_factory=dict)


@dataclass
class HistoryDraft:
    """A proposed history page. Nothing is written while errors is non-empty."""

    machine_id: str | None
    source_page_id: str
    properties: dict
    errors: list[str] = field(default_factory=list)
    failed_properties: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    last_reported_start: str | None = None
    coordinates: tuple[float, float] | None = None


@dataclass
class SnapshotResult:
    """Outcome of one snapshot execution."""

    version: str
    run_id: str
    snapshot_timestamp: str
    source_records: int
    history_created: int
    duplicates_skipped: int
    failed: int
    dry_run: bool
    would_create: int
    status: str
    duration_seconds: float
    source_database_id: str
    source_database_title: str
    destination_database_id: str
    destination_database_title: str
    mappings: list[FieldMapping]
    unmapped_source_fields: list[str]
    untouched_destination_fields: list[str]
    schema_problems: list[str]
    schema_differences: list[dict]
    failed_machine_ids: list[str]
    coordinates_preserved_via_map: int
    writes_performed: int
