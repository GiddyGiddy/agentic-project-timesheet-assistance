import json
from datetime import date

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
from sqlalchemy import select
from sqlalchemy.orm import Session

from config.settings import Settings
from src.database.models import ActivityAudit
from src.database.repository import Repository
from src.graph.state import Registry
from src.graph.workflow import build_workflow
from src.main import checked_export


@pytest.fixture
def context(tmp_path, monkeypatch):
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"servers": {}, "operations": {}}))
    settings = Settings(mcp_config_path=config, _env_file=None)
    registry = Registry.model_validate({
        "projects": [{"code": "P1", "client": "Client"}],
        "excel": {"sheet_name": "Timesheet", "header_row": 1,
                  "project_column": 1, "first_project_row": 2},
    })
    repository = Repository(f"sqlite:///{(tmp_path / 'timesheet.db').as_posix()}")
    notifications = []

    async def ingest(state, client, timezone):
        return {"raw_emails": [{"id": "email-1"}], "raw_events": [], "raw_chats": []}

    async def mapping(state, registry, settings):
        return {"mapped_entries": [
            {"project_code": "P1", "hours": 1, "summary": "Proposal", "confidence": 0.8}
        ]}

    async def notify(state, client, registry):
        notifications.append(state["target_date"])
        return {}

    monkeypatch.setattr("src.graph.workflow.ingest", ingest)
    monkeypatch.setattr("src.graph.workflow.map_activities", mapping)
    monkeypatch.setattr("src.graph.workflow.send_review", notify)
    return settings, registry, repository, notifications, tmp_path / "checkpoints.db"


@pytest.mark.parametrize("decision", ["approved", "rejected"])
async def test_approval_survives_restart_and_only_saves_approved(context, decision):
    settings, registry, repository, notifications, checkpoint = context
    config = {"configurable": {"thread_id": "daily:2026-11-15"}}
    async with AsyncSqliteSaver.from_conn_string(str(checkpoint)) as saver:
        graph = build_workflow(settings, registry, repository, saver)
        await graph.ainvoke({"target_date": "2026-11-15"}, config)
        snapshot = await graph.aget_state(config)
        assert snapshot.next == ("review",)
        assert repository.monthly_entries(2026, 11) == []
    async with AsyncSqliteSaver.from_conn_string(str(checkpoint)) as saver:
        graph = build_workflow(settings, registry, repository, saver)
        await graph.ainvoke(Command(resume={
            "status": decision,
            "entries": [{"project_code": "P1", "hours": 1.5, "summary": "Edited", "confidence": 0.9}],
        }), config)
    entries = repository.monthly_entries(2026, 11)
    assert len(entries) == (1 if decision == "approved" else 0)
    if entries:
        assert entries[0].hours == 1.5
        assert entries[0].entry_date == date(2026, 11, 15)
    with Session(repository.engine) as session:
        audits = session.scalars(select(ActivityAudit)).all()
        assert len(audits) == (1 if decision == "approved" else 0)
    assert notifications == ["2026-11-15"]


async def test_export_is_blocked_while_review_is_pending(context, tmp_path):
    settings, registry, repository, _, checkpoint = context
    settings.checkpoint_path = checkpoint
    settings.database_url = f"sqlite:///{(tmp_path / 'timesheet.db').as_posix()}"
    async with AsyncSqliteSaver.from_conn_string(str(checkpoint)) as saver:
        graph = build_workflow(settings, registry, repository, saver)
        await graph.ainvoke(
            {"target_date": "2026-11-15"},
            {"configurable": {"thread_id": "daily:2026-11-15"}},
        )
    with pytest.raises(ValueError, match="Unfinished daily workflows"):
        await checked_export(settings, registry, 2026, 11)
