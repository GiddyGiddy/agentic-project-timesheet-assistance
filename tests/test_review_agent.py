from src.agents.review_agent import send_review
from src.graph.state import Registry


async def test_review_summary_flags_low_confidence_and_requires_approval():
    registry = Registry.model_validate({
        "projects": [{"code": "P1", "client": "Client", "project_name": "Migration"}],
        "excel": {"sheet_name": "Timesheet", "header_row": 1,
                  "project_column": 1, "first_project_row": 2},
    })

    class Client:
        def operation_disabled(self, operation):
            return False

        async def call(self, operation, substitutions):
            self.operation = operation
            self.message = substitutions["message"]

    client = Client()
    await send_review({
        "target_date": "2026-11-15",
        "mapped_entries": [
            {"project_code": "P1", "hours": 1.25, "summary": "Meeting", "confidence": 0.79},
            {"project_code": "P1", "hours": 0.5, "summary": "Email", "confidence": 0.8},
        ],
    }, client, registry)
    assert client.operation == "send_review"
    assert "Client / Migration" in client.message
    assert client.message.count("[LOW CONFIDENCE]") == 1
    assert "Total: 1.75h" in client.message
    assert "not yet approved" in client.message
    assert "timesheet CLI" in client.message
