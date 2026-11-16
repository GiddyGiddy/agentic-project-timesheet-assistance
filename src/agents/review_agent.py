from langgraph.types import interrupt

from src.agents.mapper_agent import validate_entries
from src.graph.state import Registry, TimesheetState
from src.integrations.mcp_client import MCPClient


async def send_review(state: TimesheetState, client: MCPClient, registry: Registry) -> dict:
    if client.operation_disabled("send_review"):
        return {}
    entries = validate_entries(state["mapped_entries"], registry)
    projects = {project.code: project for project in registry.projects}
    lines = [f"Timesheet proposal for {state['target_date']} (not yet approved):"]
    for entry in entries:
        project = projects[entry.project_code]
        name = f" / {project.project_name}" if project.project_name else ""
        flag = " [LOW CONFIDENCE]" if entry.confidence < 0.8 else ""
        lines.append(
            f"- {entry.project_code} ({project.client}{name}): {entry.hours:.2f}h"
            f" - {entry.summary} (confidence {entry.confidence:.2f}){flag}"
        )
    if not entries:
        lines.append("- No mapped entries; check for omitted activity.")
    lines.append(f"Total: {sum(entry.hours for entry in entries):.2f}h")
    lines.append("Review and approve or reject using the timesheet CLI.")
    message = "\n".join(lines)
    await client.call("send_review", {"date": state["target_date"], "message": message})
    return {}


def await_review(state: TimesheetState, registry: Registry) -> dict:
    decision = interrupt({
        "date": state["target_date"],
        "entries": state["mapped_entries"],
        "message": "Approve, optionally with corrected entries, or reject.",
    })
    if not isinstance(decision, dict) or decision.get("status") not in ("approved", "rejected"):
        raise ValueError("Review status must be approved or rejected")
    if decision["status"] == "rejected":
        return {"approval_status": "rejected"}
    entries = decision.get("entries", state["mapped_entries"])
    if not isinstance(entries, list):
        raise ValueError("Corrected entries must be a JSON array")
    validated = validate_entries(entries, registry)
    return {
        "approval_status": "approved",
        "mapped_entries": [entry.model_dump() for entry in validated],
    }
