from datetime import date

from langgraph.graph import END, START, StateGraph

from config.settings import Settings
from src.agents.ingestion_agent import ingest
from src.agents.mapper_agent import map_activities, validate_entries
from src.agents.review_agent import await_review, send_review
from src.database.repository import Repository
from src.graph.state import Registry, TimesheetState
from src.integrations.mcp_client import MCPClient


def build_workflow(settings: Settings, registry: Registry, repository: Repository, checkpointer):
    client = MCPClient(settings.mcp_config_path, settings=settings)
    graph = StateGraph(TimesheetState)

    async def ingestion_node(state: TimesheetState) -> dict:
        return await ingest(state, client, settings.timezone)

    async def mapper_node(state: TimesheetState) -> dict:
        return await map_activities(state, registry, settings)

    async def notify_node(state: TimesheetState) -> dict:
        return await send_review(state, client, registry)

    def review_node(state: TimesheetState) -> dict:
        return await_review(state, registry)

    def save_node(state: TimesheetState) -> dict:
        repository.replace_day(
            date.fromisoformat(state["target_date"]),
            validate_entries(state["mapped_entries"], registry),
            {
                "emails": state["raw_emails"],
                "events": state["raw_events"],
                "chats": state["raw_chats"],
            },
        )
        return {}

    graph.add_node("ingest", ingestion_node)
    graph.add_node("map", mapper_node)
    graph.add_node("notify", notify_node)
    graph.add_node("review", review_node)
    graph.add_node("save", save_node)
    graph.add_edge(START, "ingest")
    graph.add_edge("ingest", "map")
    graph.add_edge("map", "notify")
    graph.add_edge("notify", "review")
    graph.add_conditional_edges(
        "review",
        lambda state: "save" if state["approval_status"] == "approved" else END,
        ["save", END],
    )
    graph.add_edge("save", END)
    return graph.compile(checkpointer=checkpointer)
