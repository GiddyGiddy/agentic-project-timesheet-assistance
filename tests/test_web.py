import json
import asyncio
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
from httpx import ASGITransport, AsyncClient

from src.database.repository import Repository
from src.graph.state import Entry
from src.web.app import create_app
from src.web.scheduler import DashboardScheduler


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in (
        "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT_NAME",
        "TENANT_ID", "CLIENT_ID", "CLIENT_SECRET", "TIMEZONE", "TEMPLATE_PATH",
    ):
        monkeypatch.delenv(key, raising=False)
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"servers": {}, "operations": {}}), encoding="utf-8")
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({
        "projects": [{"code": "P1", "client": "Client"}],
        "excel": {"sheet_name": "Timesheet", "header_row": 1,
                  "project_column": 1, "first_project_row": 2},
    }), encoding="utf-8")
    database = tmp_path / "timesheet.db"
    monkeypatch.setenv("PROJECTS_REGISTRY_PATH", str(registry))
    monkeypatch.setenv("MCP_CONFIG_PATH", str(config))
    monkeypatch.setenv("CHECKPOINT_PATH", str(tmp_path / "checkpoints.db"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database.as_posix()}")
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv("TEMPLATE_PATH", str(tmp_path / "template.xlsx"))
    with TestClient(
        create_app("private-token"),
        base_url="http://127.0.0.1:8765",
        client=("127.0.0.1", 12345),
    ) as client:
        yield client, database, registry


def test_dashboard_requires_token_and_local_origin(dashboard):
    client, _, _ = dashboard
    assert client.get("/").status_code == 200
    assert client.get("/api/setup").status_code == 403
    headers = {"X-Timesheet-Token": "private-token"}
    assert client.get("/api/setup", headers=headers).status_code == 200
    assert client.get("/api/setup", headers={**headers, "Origin": "https://evil.example"}).status_code == 403
    assert client.get("/api/setup", headers={**headers, "Host": "evil.example:8765"}).status_code == 403


def test_setup_does_not_expose_or_modify_environment(dashboard, tmp_path, monkeypatch):
    client, _, _ = dashboard
    headers = {"X-Timesheet-Token": "private-token"}
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.invalid")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key-not-real")
    monkeypatch.setenv("CLIENT_SECRET", "test-secret-not-real")
    env_file = tmp_path / ".env"
    env_file.write_text("KEEP_ME=unchanged\n", encoding="utf-8")
    response = client.get("/api/setup", headers=headers)
    assert set(response.json()) == {"mcp_operations"}
    assert "example.invalid" not in response.text
    assert "test-key-not-real" not in response.text
    assert "test-secret-not-real" not in response.text
    assert client.put("/api/setup", headers=headers, json={"timezone": "Europe/London"}).status_code == 405
    assert env_file.read_text(encoding="utf-8") == "KEEP_ME=unchanged\n"
    assert "Azure OpenAI &amp; preferences" not in client.get("/").text


def test_workiq_dashboard_sign_in_does_not_expose_tokens(dashboard, monkeypatch):
    client, _, _ = dashboard
    headers = {"X-Timesheet-Token": "private-token"}

    class FakeAuth:
        def sign_in(self):
            return None

    class FakeMCP:
        def __init__(self, path, *, settings=None):
            pass

        async def tools(self, server):
            return [type("Tool", (), {"name": "fetch"})()]

    monkeypatch.setattr("src.web.app.get_workiq_auth", lambda: FakeAuth())
    monkeypatch.setattr("src.web.app.MCPClient", FakeMCP)
    assert client.post("/api/workiq/sign-in").status_code == 403
    response = client.post("/api/workiq/sign-in", headers=headers)
    assert response.json() == {"connected": True, "tools": ["fetch"]}


def test_registry_is_validated(dashboard):
    client, _, registry_path = dashboard
    headers = {"X-Timesheet-Token": "private-token"}
    before = registry_path.read_text(encoding="utf-8")
    response = client.put("/api/registry", headers=headers, json={
        "projects": [{"code": "P1", "client": "a"}, {"code": "P1", "client": "b"}],
        "excel": {"sheet_name": "Timesheet", "header_row": 1, "project_column": 1, "first_project_row": 2},
    })
    assert response.status_code == 422
    assert registry_path.read_text(encoding="utf-8") == before
    valid_registry = json.loads(before)
    valid_registry["projects"][0]["client"] = "Updated client"
    response = client.put("/api/registry", headers=headers, json=valid_registry)
    assert response.status_code == 200
    assert json.loads(registry_path.read_text(encoding="utf-8"))["projects"][0]["client"] == "Updated client"


def test_daily_review_requires_explicit_approval(dashboard, monkeypatch):
    client, database, _ = dashboard
    headers = {"X-Timesheet-Token": "private-token"}

    async def ingest(state, client, timezone):
        return {"raw_emails": [], "raw_events": [], "raw_chats": []}

    async def mapping(state, registry, settings):
        return {"mapped_entries": [
            {"project_code": "P1", "hours": 1, "summary": "Proposed", "confidence": 0.7}
        ]}

    async def notify(state, client, registry):
        return {}

    monkeypatch.setattr("src.graph.workflow.ingest", ingest)
    monkeypatch.setattr("src.graph.workflow.map_activities", mapping)
    monkeypatch.setattr("src.graph.workflow.send_review", notify)
    response = client.post("/api/daily/2026-11-15/daily", headers=headers, json={})
    assert response.status_code == 200
    assert response.json()["pending_nodes"] == ["review"]
    assert Repository(f"sqlite:///{database.as_posix()}").monthly_entries(2026, 11) == []
    response = client.post("/api/daily/2026-11-15/approve", headers=headers, json={
        "entries": [{"project_code": "P1", "hours": 1.5, "summary": "Edited", "confidence": 0.9}]
    })
    assert response.status_code == 200
    assert response.json()["approval_status"] == "approved"
    assert Repository(f"sqlite:///{database.as_posix()}").monthly_entries(2026, 11)[0].hours == 1.5


def test_monthly_export_blocks_pending_reviews_and_preserves_template(dashboard, monkeypatch, tmp_path):
    client, database, _ = dashboard
    headers = {"X-Timesheet-Token": "private-token"}
    repository = Repository(f"sqlite:///{database.as_posix()}")
    repository.replace_day(date(2026, 11, 2), [
        Entry(project_code="P1", hours=2, summary="Approved", confidence=1)
    ], {})
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Timesheet"
    sheet["A2"] = "P1"
    for day in range(1, 32):
        sheet.cell(1, day + 1, day)
    sheet["AG2"] = "=SUM(B2:AF2)"
    template = tmp_path / "template.xlsx"
    workbook.save(template)
    original = template.read_bytes()

    async def ingest(state, client, timezone):
        return {"raw_emails": [], "raw_events": [], "raw_chats": []}

    async def mapping(state, registry, settings):
        return {"mapped_entries": [
            {"project_code": "P1", "hours": 1, "summary": "Proposal", "confidence": 0.9}
        ]}

    async def notify(state, client, registry):
        return {}

    monkeypatch.setattr("src.graph.workflow.ingest", ingest)
    monkeypatch.setattr("src.graph.workflow.map_activities", mapping)
    monkeypatch.setattr("src.graph.workflow.send_review", notify)
    assert client.post("/api/daily/2026-11-15/daily", headers=headers, json={}).status_code == 200
    overview = client.get("/api/monthly/2026-11", headers=headers).json()
    assert overview["total_hours"] == 2
    assert overview["pending_dates"] == ["2026-11-15"]
    assert 15 in overview["days_without_hours"]
    assert client.post("/api/monthly/2026-11/export", headers=headers).status_code == 400
    assert client.post("/api/daily/2026-11-15/approve", headers=headers, json={}).status_code == 200
    response = client.post("/api/monthly/2026-11/export", headers=headers)
    assert response.status_code == 200
    assert response.json()["filename"] == "timesheet-2026-11.xlsx"
    output = tmp_path / "output" / "timesheet-2026-11.xlsx"
    result = load_workbook(output).active
    assert result["C2"].value == 2
    assert result["P2"].value == 1
    assert result["AG2"].value == "=SUM(B2:AF2)"
    assert template.read_bytes() == original
    before = output.read_bytes()
    repository.replace_day(date(2026, 11, 2), [
        Entry(project_code="P1", hours=3, summary="Updated", confidence=1)
    ], {})
    response = client.post("/api/monthly/2026-11/export", headers=headers)
    assert response.status_code == 200
    assert response.json()["filename"] == "timesheet-2026-11-2.xlsx"
    latest = tmp_path / "output" / response.json()["filename"]
    assert load_workbook(latest).active["C2"].value == 3
    assert output.read_bytes() == before
    assert client.get("/api/monthly/2026-11/download").status_code == 403
    assert client.get("/api/monthly/2026-11/download", headers=headers).content == latest.read_bytes()
    assert client.get("/api/monthly/2026-13", headers=headers).status_code == 422


def test_monthly_export_creates_missing_template_from_layout(dashboard, tmp_path):
    client, database, _ = dashboard
    headers = {"X-Timesheet-Token": "private-token"}
    template = tmp_path / "template.xlsx"
    assert client.post("/api/monthly/2026-11/export", headers=headers).status_code == 400
    assert not template.exists()
    repository = Repository(f"sqlite:///{database.as_posix()}")
    repository.replace_day(date(2026, 11, 2), [
        Entry(project_code="P1", hours=2, summary="Approved", confidence=1)
    ], {})
    response = client.post("/api/monthly/2026-11/export", headers=headers)
    assert response.status_code == 200
    assert load_workbook(template)["Timesheet"]["A2"].value == "P1"
    output = tmp_path / "output" / "timesheet-2026-11.xlsx"
    assert load_workbook(output)["Timesheet"]["C2"].value == 2
    assert client.post("/api/monthly/2026-11/export", headers=headers).json()["filename"] == "timesheet-2026-11-2.xlsx"


def test_rowwise_layout_and_export_coexist_with_columnwise_export(dashboard, tmp_path):
    client, database, _ = dashboard
    headers = {"X-Timesheet-Token": "private-token"}
    repository = Repository(f"sqlite:///{database.as_posix()}")
    repository.replace_day(date(2026, 11, 2), [
        Entry(project_code="P1", hours=2, summary="Approved", confidence=1)
    ], {})
    assert client.post("/api/monthly/2026-11/export", headers=headers).status_code == 200
    old_output = (tmp_path / "output" / "timesheet-2026-11.xlsx").read_bytes()
    registry = client.get("/api/registry", headers=headers).json()
    registry["projects"][0]["project_name"] = "Example project"
    registry["excel"].update({
        "layout_mode": "rows", "date_format": "dd.mm.yyyy", "date_locale": "de-DE",
    })
    assert client.put("/api/registry", headers=headers, json=registry).status_code == 200
    assert "date_locale" not in client.get("/api/registry", headers=headers).json()["excel"]
    assert not client.get("/api/monthly/2026-11", headers=headers).json()["output_exists"]
    response = client.post("/api/monthly/2026-11/export", headers=headers)
    assert response.status_code == 200
    assert response.json()["filename"] == "timesheet-2026-11-rowwise.xlsx"
    path = tmp_path / "output" / response.json()["filename"]
    sheet = load_workbook(path).worksheets[0]
    assert sheet["B1"].value == "Example project"
    assert sheet["B2"].value == "P1"
    assert sheet["A6"].value == datetime(2026, 11, 2)
    assert sheet["A6"].number_format == "[$-409]dddd"
    assert sheet["B6"].value == datetime(2026, 11, 2)
    assert sheet["B6"].number_format == "dd.mm.yyyy"
    assert sheet["C6"].value == 2
    assert (tmp_path / "template_rowwise.xlsx").is_file()
    assert (tmp_path / "output" / "timesheet-2026-11.xlsx").read_bytes() == old_output
    assert client.get("/api/monthly/2026-11/download", headers=headers).content == path.read_bytes()
    registry["excel"]["layout_mode"] = "columns"
    assert client.put("/api/registry", headers=headers, json=registry).status_code == 200
    assert client.get("/api/monthly/2026-11/download", headers=headers).content == old_output


def test_rowwise_regeneration_finds_latest_number_even_if_original_was_removed(dashboard, tmp_path):
    client, database, _ = dashboard
    headers = {"X-Timesheet-Token": "private-token"}
    repository = Repository(f"sqlite:///{database.as_posix()}")
    repository.replace_day(date(2026, 11, 2), [
        Entry(project_code="P1", hours=2, summary="Approved", confidence=1)
    ], {})
    registry = client.get("/api/registry", headers=headers).json()
    registry["excel"]["layout_mode"] = "rows"
    assert client.put("/api/registry", headers=headers, json=registry).status_code == 200
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "timesheet-2026-11-rowwise-2.xlsx").write_bytes(b"second")
    (output_dir / "timesheet-2026-11-rowwise-10.xlsx").write_bytes(b"tenth")
    (output_dir / "timesheet-2026-11-rowwise-backup.xlsx").write_bytes(b"unrelated")
    assert client.get("/api/monthly/2026-11", headers=headers).json()["output_exists"]
    assert client.get("/api/monthly/2026-11/download", headers=headers).content == b"tenth"
    response = client.post("/api/monthly/2026-11/export", headers=headers)
    assert response.json()["filename"] == "timesheet-2026-11-rowwise-11.xlsx"
    latest = output_dir / response.json()["filename"]
    assert load_workbook(latest).worksheets[0]["C6"].value == 2
    assert client.get("/api/monthly/2026-11/download", headers=headers).content == latest.read_bytes()
    assert (output_dir / "timesheet-2026-11-rowwise-10.xlsx").read_bytes() == b"tenth"


def test_export_retries_if_another_process_creates_the_same_filename(dashboard, tmp_path, monkeypatch):
    from src.agents import excel_agent

    client, database, _ = dashboard
    headers = {"X-Timesheet-Token": "private-token"}
    repository = Repository(f"sqlite:///{database.as_posix()}")
    repository.replace_day(date(2026, 11, 2), [
        Entry(project_code="P1", hours=2, summary="Approved", confidence=1)
    ], {})
    original_write = excel_agent.write_timesheet
    raced = False

    def competing_export(template, output, *args):
        nonlocal raced
        if not raced:
            raced = True
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"other export")
            raise FileExistsError(output)
        return original_write(template, output, *args)

    monkeypatch.setattr(excel_agent, "write_timesheet", competing_export)
    response = client.post("/api/monthly/2026-11/export", headers=headers)
    assert response.status_code == 200
    assert response.json()["filename"] == "timesheet-2026-11-2.xlsx"
    assert (tmp_path / "output" / "timesheet-2026-11.xlsx").read_bytes() == b"other export"
    assert load_workbook(tmp_path / "output" / response.json()["filename"]).active["C2"].value == 2


def test_scheduler_start_stop(dashboard):
    client, _, _ = dashboard
    headers = {"X-Timesheet-Token": "private-token"}
    assert client.get("/api/scheduler", headers=headers).json()["running"] is False
    result = client.post("/api/scheduler/start", headers=headers).json()
    assert result["running"] is True
    assert result["timezone"] == "Europe/Berlin"
    assert datetime.fromisoformat(result["next_runs"]["daily"]).hour == 18
    assert datetime.fromisoformat(result["next_runs"]["month-end"]).minute == 30
    assert client.post("/api/scheduler/start", headers=headers).status_code == 409
    assert client.post("/api/scheduler/stop", headers=headers).json()["running"] is False
    assert client.post("/api/scheduler/stop", headers=headers).status_code == 409
    assert client.post("/api/scheduler/start", headers=headers).json()["running"] is True
    assert client.post("/api/scheduler/stop", headers=headers).json()["running"] is False


async def test_scheduler_stop_waits_for_active_work(dashboard, monkeypatch):
    started = asyncio.Event()
    finish = asyncio.Event()

    async def run_daily(*args, **kwargs):
        started.set()
        await finish.wait()

    monkeypatch.setattr("src.web.scheduler.graph_action", run_daily)
    scheduler = DashboardScheduler(asyncio.Lock())
    await scheduler.start()
    running = asyncio.create_task(scheduler._daily_job())
    await started.wait()
    stopping = asyncio.create_task(scheduler.stop())
    await asyncio.sleep(0)
    assert not stopping.done()
    finish.set()
    await running
    await stopping
    assert scheduler.status()["last_runs"]["daily"]["status"] == "succeeded"
    assert not scheduler.running


async def test_scheduler_reports_job_failure(dashboard, monkeypatch):
    async def fail_daily(*args, **kwargs):
        raise ValueError("A daily run is already pending")

    monkeypatch.setattr("src.web.scheduler.graph_action", fail_daily)
    scheduler = DashboardScheduler(asyncio.Lock())
    await scheduler.start()
    try:
        await scheduler._daily_job()
        result = scheduler.status()["last_runs"]["daily"]
        assert result["status"] == "failed"
        assert "already pending" in result["message"]
    finally:
        await scheduler.stop()


async def test_exit_blocks_new_requests_and_drains_active_operation(dashboard, monkeypatch):
    started, finish, exited = asyncio.Event(), asyncio.Event(), asyncio.Event()
    callbacks = []

    async def active_work(*args, **kwargs):
        started.set()
        await finish.wait()
        return {"finished": True}

    def request_exit():
        callbacks.append(True)
        exited.set()

    monkeypatch.setattr("src.web.app.graph_action", active_work)
    app = create_app("exit-token", on_shutdown=request_exit)
    async with app.router.lifespan_context(app), AsyncClient(
        transport=ASGITransport(app, client=("127.0.0.1", 12345)),
        base_url="http://127.0.0.1:8765", headers={"X-Timesheet-Token": "exit-token"},
    ) as client:
        assert (await client.post("/api/lifecycle", headers={"X-Timesheet-Token": ""})).status_code == 403
        assert (await client.post("/api/lifecycle", headers={"Origin": "https://untrusted.invalid"})).status_code == 403
        work = asyncio.create_task(client.post("/api/daily/2026-11-19/daily", json={}))
        await asyncio.wait_for(started.wait(), 2)
        try:
            assert (await client.post("/api/lifecycle")).status_code == 202
            assert (await client.get("/api/lifecycle")).json()["stopping"]
            assert not exited.is_set()
            assert (await client.post("/api/scheduler/start")).status_code == 409
            assert (await client.get("/api/registry")).status_code == 409
            assert (await client.post("/api/lifecycle")).status_code == 202
        finally:
            finish.set()
            assert (await work).status_code == 200
        await asyncio.wait_for(exited.wait(), 2)
        assert callbacks == [True]


def test_unmanaged_app_cannot_exit(dashboard):
    client, _, _ = dashboard
    headers = {"X-Timesheet-Token": "private-token"}
    assert not client.get("/api/lifecycle", headers=headers).json()["exit_available"]
    assert client.post("/api/lifecycle", headers=headers).status_code == 405
    assert client.get("/api/registry", headers=headers).status_code == 200


async def test_scheduler_close_drains_active_job_skips_queued_job_and_prevents_restart(dashboard, monkeypatch):
    started, finish = asyncio.Event(), asyncio.Event()
    calls = []

    async def daily(*args, **kwargs):
        calls.append(True)
        started.set()
        await finish.wait()

    monkeypatch.setattr("src.web.scheduler.graph_action", daily)
    scheduler = DashboardScheduler(asyncio.Lock())
    await scheduler.start()
    active = asyncio.create_task(scheduler._daily_job())
    await asyncio.wait_for(started.wait(), 2)
    queued = asyncio.create_task(scheduler._daily_job())
    closing = asyncio.create_task(scheduler.close())
    await asyncio.sleep(0)
    try:
        assert not closing.done()
    finally:
        finish.set()
        await asyncio.gather(active, queued, closing)
    assert calls == [True]
    assert not scheduler.running
    with pytest.raises(ValueError, match="shutting down"):
        await scheduler.start()
    await scheduler.close()
