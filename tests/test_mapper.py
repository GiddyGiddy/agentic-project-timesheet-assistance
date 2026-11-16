import pytest

from config.settings import Settings
from src.agents.mapper_agent import map_activities, validate_entries
from src.graph.state import MappedEntries, Registry


@pytest.fixture
def registry():
    return Registry.model_validate({
        "projects": [{"code": "P1", "client": "Client", "domains": ["client.example"]}],
        "excel": {
            "sheet_name": "Timesheet", "header_row": 1,
            "project_column": 1, "first_project_row": 2,
        },
    })


def test_validate_rejects_unknown_projects_and_invalid_hours(registry):
    with pytest.raises(ValueError, match="Unknown project"):
        validate_entries([{"project_code": "other", "hours": 1, "summary": "x", "confidence": 1}], registry)
    with pytest.raises(ValueError, match="0.25"):
        validate_entries([{"project_code": "P1", "hours": 1.1, "summary": "x", "confidence": 1}], registry)
    with pytest.raises(ValueError, match="exceed 24"):
        validate_entries([
            {"project_code": "P1", "hours": 13, "summary": "x", "confidence": 1},
            {"project_code": "P1", "hours": 12, "summary": "y", "confidence": 1},
        ], registry)


async def test_mapper_passes_activities_and_validates_result(monkeypatch, registry):
    observed = {}

    class FakeModel:
        def __init__(self, **kwargs):
            pass

        def with_structured_output(self, schema, method):
            assert schema is MappedEntries
            return self

        async def ainvoke(self, messages):
            observed["payload"] = messages[1].content
            return MappedEntries.model_validate({"entries": [
                {"project_code": "P1", "hours": 0.75, "summary": "Meeting", "confidence": 0.8}
            ]})

    monkeypatch.setattr("src.agents.mapper_agent.AzureChatOpenAI", FakeModel)
    settings = Settings(
        azure_openai_api_key="test-key",
        azure_openai_endpoint="https://example.invalid",
        azure_openai_deployment_name="test",
        _env_file=None,
    )
    result = await map_activities({
        "target_date": "2026-11-15",
        "raw_emails": [{"subject": "client discussion"}],
        "raw_events": [{"duration_minutes": 45}],
        "raw_chats": [],
    }, registry, settings)
    assert "client discussion" in observed["payload"]
    assert result["mapped_entries"][0]["hours"] == 0.75
