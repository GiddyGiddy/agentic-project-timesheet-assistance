from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from src.graph.state import TimesheetState
from src.integrations.mcp_client import MCPClient, parse_records


async def ingest(state: TimesheetState, client: MCPClient, timezone: str) -> dict:
    day = date.fromisoformat(state["target_date"])
    zone = ZoneInfo(timezone)
    values = {
        "date": day.isoformat(),
        "start_datetime": datetime.combine(day, time.min, zone).isoformat(),
        "end_datetime": datetime.combine(day + timedelta(days=1), time.min, zone).isoformat(),
        "timezone": timezone,
    }
    data = {}
    for operation in ("emails", "events", "chats"):
        data[f"raw_{operation}"] = parse_records(
            await client.call(operation, values), operation
        )
    return data
