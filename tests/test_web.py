import json
import asyncio
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

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
        def __init__(self, path):
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
    assert client.post("/api/monthly/2026-11/export", headers=headers).status_code == 409
    assert client.get("/api/monthly/2026-11/download").status_code == 403
    assert client.get("/api/monthly/2026-11/download", headers=headers).content == output.read_bytes()
    assert client.get("/api/monthly/2026-13", headers=headers).status_code == 422


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
