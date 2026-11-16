"""Synthetic Microsoft 365 activity for the sandbox sample run. All data is fictional."""
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("sample-m365")
OUTBOX = Path(__file__).parent / "review_message.txt"
TZ = "+01:00"


@mcp.tool()
def emails(day: str) -> list[dict[str, Any]]:
    return [
        {"type": "email", "direction": "sent", "subject": "Re: ALPHA release notes",
         "from": "me@giddy.example", "to": ["dana@alpha-client.example"], "cc": [],
         "at": f"{day}T11:20:00{TZ}", "preview": "Attached the updated release notes and the open points for Monday."},
        {"type": "email", "direction": "received", "subject": "BETA API questions",
         "from": "lee@beta-client.example", "to": ["me@giddy.example"], "cc": [],
         "at": f"{day}T13:40:00{TZ}", "preview": "Can we clarify the pagination behaviour before the design review?"},
        {"type": "email", "direction": "received", "subject": "Weekly newsletter",
         "from": "news@vendor-marketing.example", "to": ["me@giddy.example"], "cc": [],
         "at": f"{day}T07:05:00{TZ}", "preview": "Ten tips for better dashboards."},
    ]


@mcp.tool()
def events(day: str) -> list[dict[str, Any]]:
    return [
        {"type": "meeting", "subject": "ALPHA weekly sync", "start": f"{day}T09:00:00{TZ}",
         "end": f"{day}T10:00:00{TZ}", "duration_minutes": 60, "is_all_day": False,
         "organizer": "pm@alpha-client.example", "attendees": ["dana@alpha-client.example"],
         "attendee_count": 3, "location": "Teams meeting", "response": "accepted"},
        {"type": "meeting", "subject": "Team lunch", "start": f"{day}T12:00:00{TZ}",
         "end": f"{day}T13:00:00{TZ}", "duration_minutes": 60, "is_all_day": False,
         "organizer": "office@giddy.example", "attendees": [], "attendee_count": 8,
         "location": "Canteen", "response": "accepted"},
        {"type": "meeting", "subject": "BETA design review", "start": f"{day}T14:00:00{TZ}",
         "end": f"{day}T15:30:00{TZ}", "duration_minutes": 90, "is_all_day": False,
         "organizer": "lee@beta-client.example", "attendees": ["pm@beta-client.example"],
         "attendee_count": 4, "location": "Teams meeting", "response": "accepted"},
    ]


@mcp.tool()
def chats(day: str) -> list[dict[str, Any]]:
    return [
        {"type": "chat_message", "chat": "gamma-migration", "chat_type": "group", "from": "Sam",
         "at": f"{day}T16:05:00{TZ}", "preview": "Did the data migration scripts finish on staging?"},
        {"type": "chat_message", "chat": "gamma-migration", "chat_type": "group", "from": "Me",
         "at": f"{day}T16:40:00{TZ}", "preview": "Reran them after fixing the date conversion; all tables are consistent now."},
    ]


@mcp.tool()
def send_review(message: str) -> str:
    OUTBOX.write_text(message, encoding="utf-8")
    return "stored"


if __name__ == "__main__":
    mcp.run()
