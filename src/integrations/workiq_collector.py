import html
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import quote
from zoneinfo import ZoneInfo

Invoke = Callable[[str, dict[str, Any]], Awaitable[Any]]

EMAIL_CAP = 100
CALENDAR_CAP = 100
CHAT_MESSAGE_CAP = 10
CHAT_LIST_CAP = 50
MIN_WINDOW = timedelta(minutes=5)
PREVIEW_LIMIT = 300
MAX_ATTENDEES = 25
MAX_REQUESTS = 300


class IncompleteCollectionError(RuntimeError):
    pass


def _json_payload(result: Any) -> dict[str, Any]:
    if isinstance(result, tuple):
        result = result[0]
    if isinstance(result, str):
        result = json.loads(result)
    elif isinstance(result, list):
        blocks = [
            json.loads(block["text"]) for block in result
            if isinstance(block, dict) and block.get("type") == "text" and "text" in block
        ]
        if len(blocks) != 1 or len(blocks) != len(result):
            raise ValueError("Work IQ returned an unexpected response format")
        result = blocks[0]
    if not isinstance(result, dict):
        raise ValueError("Work IQ returned an unexpected response format")
    return result


def _entity(payload: dict[str, Any], label: str) -> dict[str, Any]:
    if isinstance(payload.get("results"), list):
        if len(payload["results"]) != 1:
            raise ValueError("Work IQ returned an unexpected number of results")
        payload = payload["results"][0]
    status = payload.get("statusCode")
    if status is not None and status != 200:
        code = payload.get("errorCode")
        suffix = f" ({code})" if isinstance(code, str) else ""
        raise RuntimeError(f"Work IQ returned HTTP {status} for {label}{suffix}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError(f"Work IQ returned no data for {label}")
    return data


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Work IQ time windows require UTC offsets")
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: str, zone_name: str | None = None) -> datetime:
    parsed = datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", value))
    if parsed.tzinfo is None:
        if zone_name in (None, "UTC", "Etc/UTC"):
            zone = timezone.utc
        else:
            try:
                zone = ZoneInfo(zone_name)
            except Exception as exc:
                raise ValueError(f"Unsupported calendar time zone: {zone_name}") from exc
        parsed = parsed.replace(tzinfo=zone)
    return parsed.astimezone(timezone.utc)


def _local(value: datetime, zone: ZoneInfo) -> str:
    return value.astimezone(zone).replace(microsecond=0).isoformat()


def _query(params: dict[str, Any]) -> str:
    return "&".join(f"{key}={quote(str(value), safe=':,/()')}" for key, value in params.items())


def _clip(text: str | None) -> str:
    cleaned = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:PREVIEW_LIMIT]


def _address(value: dict[str, Any] | None) -> str | None:
    email = (value or {}).get("emailAddress") or {}
    return email.get("address") or email.get("name")


def _addresses(values: list[dict[str, Any]] | None) -> list[str]:
    return [item for item in (_address(value) for value in values or []) if item]


class WorkIQCollector:
    def __init__(self, invoke: Invoke, max_requests: int = MAX_REQUESTS):
        self.invoke = invoke
        self.max_requests = max_requests
        self.requests = 0

    async def collect(self, resource: str, substitutions: dict[str, str]) -> list[dict[str, Any]]:
        start = _utc(substitutions["start_datetime"])
        end = _utc(substitutions["end_datetime"])
        if end <= start:
            raise ValueError("The collection window must end after it starts")
        zone = ZoneInfo(substitutions["timezone"])
        collectors = {"emails": self._emails, "events": self._events, "chats": self._chats}
        if resource not in collectors:
            raise ValueError(f"Unsupported Work IQ resource: {resource}")
        records = await collectors[resource](start, end, zone)
        return sorted(records, key=lambda record: record.get("at") or record.get("start") or "")

    async def _get(self, path: str, label: str) -> dict[str, Any]:
        self.requests += 1
        if self.requests > self.max_requests:
            raise IncompleteCollectionError("Too many Work IQ requests were needed; collection stopped")
        return _entity(_json_payload(await self.invoke("fetch", {"entityUrls": [path]})), label)

    async def _windowed(self, label, cap, start, end, fetch) -> list[dict[str, Any]]:
        data = await fetch(start, end)
        items = data.get("value")
        if not isinstance(items, list):
            raise ValueError(f"Work IQ returned an unexpected {label} list")
        if len(items) < cap and "@odata.nextLink" not in data:
            return items
        if end - start <= MIN_WINDOW:
            raise IncompleteCollectionError(
                f"Too many {label} in one {int(MIN_WINDOW.total_seconds() // 60)}-minute window "
                "to collect them completely"
            )
        middle = start + timedelta(seconds=int((end - start).total_seconds() // 2))
        return (
            await self._windowed(label, cap, start, middle, fetch)
            + await self._windowed(label, cap, middle, end, fetch)
        )

    async def _emails(self, start, end, zone) -> list[dict[str, Any]]:
        select = "subject,from,toRecipients,ccRecipients,receivedDateTime,sentDateTime,bodyPreview,isDraft"
        records = []
        for folder, field, direction in (
            ("inbox", "receivedDateTime", "received"), ("sentitems", "sentDateTime", "sent")
        ):
            async def fetch(window_start, window_end, folder=folder, field=field):
                path = f"/me/mailFolders/{folder}/messages?" + _query({
                    "$select": select,
                    "$filter": f"{field} ge {_stamp(window_start)} and {field} lt {_stamp(window_end)}",
                    "$orderby": f"{field} desc",
                    "$top": EMAIL_CAP,
                })
                return await self._get(path, f"{direction} emails")

            for item in await self._windowed(f"{direction} emails", EMAIL_CAP, start, end, fetch):
                if item.get("isDraft") or not item.get(field):
                    continue
                records.append({
                    "type": "email",
                    "direction": direction,
                    "subject": item.get("subject") or "",
                    "from": _address(item.get("from")),
                    "to": _addresses(item.get("toRecipients")),
                    "cc": _addresses(item.get("ccRecipients")),
                    "at": _local(_parse_time(item[field]), zone),
                    "preview": _clip(item.get("bodyPreview")),
                })
        return records

    async def _events(self, start, end, zone) -> list[dict[str, Any]]:
        select = "id,subject,start,end,isAllDay,isCancelled,showAs,organizer,attendees,location,responseStatus"

        async def fetch(window_start, window_end):
            path = "/me/calendarView?" + _query({
                "startDateTime": _stamp(window_start),
                "endDateTime": _stamp(window_end),
                "$select": select,
                "$orderby": "start/dateTime",
                "$top": CALENDAR_CAP,
            })
            return await self._get(path, "calendar events")

        seen = set()
        records = []
        for item in await self._windowed("calendar events", CALENDAR_CAP, start, end, fetch):
            event_start = _parse_time(item["start"]["dateTime"], item["start"].get("timeZone"))
            event_end = _parse_time(item["end"]["dateTime"], item["end"].get("timeZone"))
            key = item.get("id") or (item.get("subject"), event_start)
            response = (item.get("responseStatus") or {}).get("response")
            if key in seen or item.get("isCancelled") or response == "declined":
                continue
            seen.add(key)
            all_day = bool(item.get("isAllDay"))
            overlap = (min(event_end, end) - max(event_start, start)).total_seconds()
            if overlap <= 0 and not all_day:
                continue
            attendees = _addresses(item.get("attendees"))
            records.append({
                "type": "meeting",
                "subject": item.get("subject") or "",
                "start": _local(event_start, zone),
                "end": _local(event_end, zone),
                "duration_minutes": None if all_day else round(overlap / 60),
                "is_all_day": all_day,
                "organizer": _address(item.get("organizer")),
                "attendees": attendees[:MAX_ATTENDEES],
                "attendee_count": len(attendees),
                "location": (item.get("location") or {}).get("displayName") or "",
                "response": response,
            })
        return records

    async def _chats(self, start, end, zone) -> list[dict[str, Any]]:
        listing = await self._get("/me/chats?" + _query({
            "$select": "id,topic,chatType",
            "$expand": "lastMessagePreview",
            "$filter": f"lastMessagePreview/createdDateTime ge {_stamp(start)}",
            "$orderby": "lastMessagePreview/createdDateTime desc",
            "$top": CHAT_LIST_CAP,
        }), "Teams chats")
        chats = listing.get("value")
        if not isinstance(chats, list):
            raise ValueError("Work IQ returned an unexpected Teams chat list")
        if "@odata.nextLink" in listing or len(chats) >= CHAT_LIST_CAP:
            raise IncompleteCollectionError(
                "More Teams chats were active than can be listed completely"
            )
        records = []
        for chat in chats:
            chat_id = chat.get("id")
            if not chat_id:
                continue
            label = chat.get("topic") or chat.get("chatType") or "chat"

            async def fetch(window_start, window_end, chat_id=chat_id):
                path = f"/me/chats/{quote(chat_id, safe=':@.-_')}/messages?" + _query({
                    "$filter": (
                        f"createdDateTime ge {_stamp(window_start)} "
                        f"and createdDateTime lt {_stamp(window_end)}"
                    ),
                    "$orderby": "createdDateTime desc",
                    "$top": CHAT_MESSAGE_CAP,
                })
                return await self._get(path, "Teams chat messages")

            for item in await self._windowed("Teams chat messages", CHAT_MESSAGE_CAP, start, end, fetch):
                if item.get("messageType") not in (None, "message") or item.get("deletedDateTime"):
                    continue
                sender = ((item.get("from") or {}).get("user") or {}).get("displayName")
                records.append({
                    "type": "chat_message",
                    "chat": label,
                    "chat_type": chat.get("chatType"),
                    "from": sender,
                    "at": _local(_parse_time(item["createdDateTime"]), zone),
                    "preview": _clip((item.get("body") or {}).get("content")),
                })
        return records
