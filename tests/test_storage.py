import json
import sqlite3
import sys
from contextlib import closing
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url

from config.settings import Settings
from config.storage import import_user_data, initialize_user_storage
from src.database.repository import Repository
from src.graph.state import Entry
from src.integrations.mcp_client import MCPClient
from src.main import graph_action, load_registry, main
from src.web.app import create_app


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)


def test_development_settings_and_mcp_still_read_local_dotenv(tmp_path):
    (tmp_path / ".env").write_text(
        "AZURE_OPENAI_API_KEY=development-only\nMCP_TEST_URL=https://example.invalid/mcp\n",
        encoding="utf-8",
    )
    settings = Settings()
    assert settings.timesheet_data_dir is None
    assert settings.database_url == "sqlite:///data/timesheet.db"
    assert settings.projects_registry_path == Path("config") / "projects_registry.json"
    assert settings.azure_openai_api_key.get_secret_value() == "development-only"
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({
        "servers": {"test": {"transport": "http", "url": "${MCP_TEST_URL}"}},
        "operations": {},
    }), encoding="utf-8")
    assert MCPClient(config).servers["test"]["url"] == "https://example.invalid/mcp"


def test_profile_ignores_working_directory_dotenv_and_roots_relative_overrides(tmp_path, monkeypatch):
    root = tmp_path / "profile"
    monkeypatch.setenv("TIMESHEET_DATA_DIR", str(root))
    (tmp_path / ".env").write_text(
        "AZURE_OPENAI_API_KEY=must-not-be-read\nDATABASE_URL=sqlite:///wrong.db\n"
        "MCP_TEST_URL=https://must-not-be-read.invalid\n",
        encoding="utf-8",
    )
    settings = Settings(output_dir=Path("custom-output"))
    assert settings.azure_openai_api_key is None
    assert settings.checkpoint_path == root / "data" / "checkpoints.db"
    assert settings.output_dir == root / "custom-output"
    assert make_url(Settings(database_url="sqlite:///custom.db").database_url).database == str(root / "custom.db")
    assert Settings(database_url="sqlite:///:memory:").database_url == "sqlite:///:memory:"
    repository = Repository(settings.database_url)
    try:
        assert repository.engine.url.database == str(root / "data" / "timesheet.db")
    finally:
        repository.engine.dispose()
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({
        "servers": {"test": {"transport": "http", "url": "${MCP_TEST_URL}"}},
        "operations": {},
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="MCP_TEST_URL"):
        MCPClient(config)
    monkeypatch.delenv("TIMESHEET_DATA_DIR")
    with pytest.raises(ValueError, match="MCP_TEST_URL"):
        MCPClient(config, settings=settings)
    assert not (tmp_path / "wrong.db").exists()


def test_frozen_profile_is_seeded_without_copying_working_directory_data(tmp_path, monkeypatch):
    appdata = tmp_path / "local-app-data"
    monkeypatch.setenv("LOCALAPPDATA", str(appdata))
    monkeypatch.setattr(sys, "frozen", True)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "timesheet.db").write_bytes(b"existing-development-data")
    settings = Settings()
    root = appdata / "TimesheetAssistant"
    assert settings.timesheet_data_dir == root
    initialize_user_storage(settings)
    assert settings.mcp_config_path.is_file()
    assert not (root / "data" / "timesheet.db").exists()
    assert not settings.template_path.exists()
    registry = json.loads(settings.projects_registry_path.read_text(encoding="utf-8"))
    registry["projects"][0]["client"] = "User-edited client"
    settings.projects_registry_path.write_text(json.dumps(registry), encoding="utf-8")
    initialize_user_storage(Settings())
    assert load_registry(settings.projects_registry_path).projects[0].client == "User-edited client"
    assert (tmp_path / "data" / "timesheet.db").read_bytes() == b"existing-development-data"


def test_profile_paths_fail_explicitly_when_not_absolute_or_appdata_missing(monkeypatch):
    with pytest.raises(ValueError, match="absolute"):
        Settings(timesheet_data_dir=Path("relative"))
    monkeypatch.setattr(sys, "frozen", True)
    with pytest.raises(ValueError, match="LOCALAPPDATA"):
        Settings()


def make_legacy_data(root):
    settings = Settings(timesheet_data_dir=root)
    initialize_user_storage(settings)
    for path in (root / "data" / "timesheet.db", settings.checkpoint_path):
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TABLE sample (value TEXT)")
            connection.execute("INSERT INTO sample VALUES ('committed')")
            connection.commit()
    return settings


def test_import_cli_snapshots_wal_and_copies_only_allowlisted_data(tmp_path, monkeypatch, capsys):
    source = tmp_path / "legacy"
    target = tmp_path / "new-profile"
    settings = make_legacy_data(source)
    (source / ".env").write_text("AZURE_OPENAI_API_KEY=do-not-import", encoding="utf-8")
    settings.mcp_config_path.write_text('{"private": "do-not-import"}', encoding="utf-8")
    settings.template_path.write_bytes(b"explicitly-selected-template")
    (source / "output" / "timesheet-2026-11.xlsx").write_bytes(b"export")
    (source / "output" / "unrelated.txt").write_text("do-not-import", encoding="utf-8")
    with closing(sqlite3.connect(source / "data" / "timesheet.db")) as original:
        original.execute("PRAGMA journal_mode=WAL")
        original.execute("INSERT INTO sample VALUES ('in-wal')")
        original.commit()
        monkeypatch.setattr(sys, "argv", [
            "timesheet", "import-data", "--source", str(source),
            "--destination", str(target), "--source-closed",
        ])
        main()
        with closing(sqlite3.connect(target / "data" / "timesheet.db")) as imported:
            assert imported.execute("SELECT value FROM sample ORDER BY rowid").fetchall() == [
                ("committed",), ("in-wal",),
            ]
        assert original.execute("SELECT COUNT(*) FROM sample").fetchone() == (2,)
    assert "Imported data into" in capsys.readouterr().out
    assert not (target / ".env").exists()
    assert not (target / "output" / "unrelated.txt").exists()
    assert (target / "output" / "timesheet-2026-11.xlsx").read_bytes() == b"export"
    assert (target / "data" / "templates" / "timesheet_template.xlsx").read_bytes() == b"explicitly-selected-template"
    assert "do-not-import" not in (target / "config" / "mcp_servers.json").read_text(encoding="utf-8")


@pytest.mark.parametrize("failure", ["invalid_database", "missing_checkpoint", "invalid_registry"])
def test_import_failure_does_not_publish_partial_profile(tmp_path, failure):
    source = tmp_path / "legacy"
    target = tmp_path / "new-profile"
    settings = make_legacy_data(source)
    if failure == "invalid_database":
        (source / "data" / "timesheet.db").write_bytes(b"not-a-database")
    elif failure == "missing_checkpoint":
        settings.checkpoint_path.unlink()
    else:
        settings.projects_registry_path.write_text('{"projects": []}', encoding="utf-8")
    with pytest.raises((ValueError, sqlite3.Error, FileNotFoundError)):
        import_user_data(source, target)
    assert not target.exists()
    assert not list(tmp_path.glob(".timesheet-import-*"))
    assert source.is_dir()


def test_import_refuses_existing_or_nested_destinations(tmp_path):
    source = tmp_path / "legacy"
    make_legacy_data(source)
    target = tmp_path / "existing-profile"
    target.mkdir()
    sentinel = target / "keep.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    with pytest.raises(FileExistsError, match="already exists"):
        import_user_data(source, target)
    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    with pytest.raises(ValueError, match="non-nested"):
        import_user_data(source, source / "nested-profile")


async def test_import_preserves_approved_entries_and_pending_review(tmp_path, monkeypatch):
    source = tmp_path / "legacy"
    target = tmp_path / "imported-profile"
    settings = Settings(timesheet_data_dir=source)
    initialize_user_storage(settings)
    registry = load_registry(settings.projects_registry_path)
    repository = Repository(settings.database_url)
    try:
        repository.replace_day(date(2026, 11, 14), [
            Entry(project_code="EXAMPLE-001", hours=2, summary="Approved", confidence=1),
        ], {})
    finally:
        repository.engine.dispose()

    async def ingest(state, client, timezone):
        return {"raw_emails": [], "raw_events": [], "raw_chats": []}

    async def mapping(state, registry, settings):
        return {"mapped_entries": [
            {"project_code": "EXAMPLE-001", "hours": 1, "summary": "Pending", "confidence": 0.9},
        ]}

    monkeypatch.setattr("src.graph.workflow.ingest", ingest)
    monkeypatch.setattr("src.graph.workflow.map_activities", mapping)
    await graph_action(settings, registry, date(2026, 11, 15), "daily", verbose=False)
    import_user_data(source, target)
    imported_settings = Settings(timesheet_data_dir=target)
    imported_registry = load_registry(imported_settings.projects_registry_path)
    pending = await graph_action(imported_settings, imported_registry, date(2026, 11, 15), "show", verbose=False)
    assert pending["pending_nodes"] == ["review"]
    assert pending["approval_status"] is None
    imported_repository = Repository(imported_settings.database_url)
    try:
        saved = imported_repository.monthly_entries(2026, 11)
        assert [(entry.entry_date, entry.hours) for entry in saved] == [(date(2026, 11, 14), 2)]
    finally:
        imported_repository.engine.dispose()


def test_dashboard_profile_registry_survives_restart_from_another_directory(tmp_path, monkeypatch):
    root = tmp_path / "profile"
    monkeypatch.setenv("TIMESHEET_DATA_DIR", str(root))
    headers = {"X-Timesheet-Token": "test-token"}
    for attempt in range(2):
        with TestClient(
            create_app("test-token"), base_url="http://127.0.0.1:8765",
            client=("127.0.0.1", 12345),
        ) as client:
            response = client.get("/api/registry", headers=headers)
            assert response.status_code == 200
            registry = response.json()
            if attempt == 0:
                registry["projects"][0]["client"] = "Persisted client"
                assert client.put("/api/registry", headers=headers, json=registry).status_code == 200
            else:
                assert registry["projects"][0]["client"] == "Persisted client"
        elsewhere = tmp_path / "other-working-directory"
        elsewhere.mkdir(exist_ok=True)
        monkeypatch.chdir(elsewhere)
