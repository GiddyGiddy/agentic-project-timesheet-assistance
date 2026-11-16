import json

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import AzureChatOpenAI
from pydantic import SecretStr

from config.settings import Settings
from src.graph.prompts import MAPPER_SYSTEM_PROMPT
from src.graph.state import Entry, MappedEntries, Registry, TimesheetState


def validate_entries(entries: list[dict], registry: Registry) -> list[Entry]:
    validated = [Entry.model_validate(entry) for entry in entries]
    codes = {project.code for project in registry.projects}
    unknown = {entry.project_code for entry in validated} - codes
    if unknown:
        raise ValueError(f"Unknown project codes: {', '.join(sorted(unknown))}")
    if sum(entry.hours for entry in validated) > 24:
        raise ValueError("Total hours for one day cannot exceed 24")
    return validated


async def map_activities(
    state: TimesheetState, registry: Registry, settings: Settings
) -> dict:
    if not all(
        [settings.azure_openai_api_key, settings.azure_openai_endpoint,
         settings.azure_openai_deployment_name]
    ):
        raise ValueError("Azure OpenAI endpoint, deployment, and API key are required")
    model = AzureChatOpenAI(
        azure_endpoint=settings.azure_openai_endpoint,
        azure_deployment=settings.azure_openai_deployment_name,
        api_version=settings.azure_openai_api_version,
        api_key=SecretStr(settings.azure_openai_api_key.get_secret_value()),
        temperature=0,
    )
    payload = {
        "date": state["target_date"],
        "projects": [project.model_dump() for project in registry.projects],
        "emails": state["raw_emails"],
        "events": state["raw_events"],
        "chats": state["raw_chats"],
    }
    result = await model.with_structured_output(
        MappedEntries, method="function_calling"
    ).ainvoke([
        SystemMessage(content=MAPPER_SYSTEM_PROMPT),
        HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
    ])
    validated = validate_entries(
        [entry.model_dump() for entry in result.entries], registry
    )
    return {"mapped_entries": [entry.model_dump() for entry in validated]}
