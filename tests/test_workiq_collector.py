import json
from datetime import datetime, timedelta, timezone

import pytest

from src.integrations import workiq_collector
from src.integrations.mcp_client import MCPClient, parse_records
from src.integrations.workiq_collector import IncompleteCollectionError, WorkIQCollector

SUBS = {
    "start_datetime": "2026-11-15T00:00:00+01:00",
    "end_datetime": "2026-11-16T00:00:00+01:00",
    "timezone": "Europe/Berlin",
}


def blocks(data, status=200):
    return [{"type": "text", "text": json.dumps({"results": [{"data": data, "statusCode": status}]})}]


def make_collector(handler):
    calls = []

    async def invoke(tool, args):
        assert tool == "fetch"
        calls.append(args["entityUrls"][0])
        return handler(args["entityUrls"][0])

    return WorkIQCollector(invoke), calls


async def test_emails_use_utc_windows_and_minimise_content():
    def handler(url):
        assert "receivedDateTime%20ge%202026-11-14T23:00:00Z" in url or "sentDateTime%20ge%202026-11-14T23:00:00Z" in url
        if "/inbox/" in url:
            return blocks({"value": [
                {"subject": "Plan", "from": {"emailAddress": {"address": "a@client.com"}},
                 "toRecipients": [{"emailAddress": {"address": "me@example.com"}}], "ccRecipients": [],
                 "receivedDateTime": "2026-11-15T08:30:00.1234567Z",
                 "bodyPreview": "<p>Hello &amp; welcome " + "x" * 400 + "</p>", "isDraft": False},
                {"subject": "Draft", "receivedDateTime": "2026-11-15T09:00:00Z", "isDraft": True},
            ]})
        return blocks({"value": [
            {"subject": "Reply", "from": {"emailAddress": {"address": "me@example.com"}},
             "toRecipients": [{"emailAddress": {"address": "a@client.com"}}],
             "sentDateTime": "2026-11-15T10:00:00Z", "bodyPreview": "Done", "isDraft": False},
        ]})

    collector, _ = make_collector(handler)
    records = await collector.collect("emails", SUBS)
    assert [(r["direction"], r["subject"]) for r in records] == [("received", "Plan"), ("sent", "Reply")]
    assert records[0]["at"] == "2026-11-15T09:30:00+01:00"
    assert records[0]["preview"].startswith("Hello & welcome ") and len(records[0]["preview"]) == 300
    assert records[0]["from"] == "a@client.com" and records[1]["to"] == ["a@client.com"]


async def test_events_filter_dedupe_and_clip_to_the_day():
    items = [
        {"id": "1", "subject": "Spans midnight", "isAllDay": False,
         "start": {"dateTime": "2026-11-14T22:30:00.0000000", "timeZone": "UTC"},
         "end": {"dateTime": "2026-11-15T00:30:00.0000000", "timeZone": "UTC"}},
        {"id": "2", "subject": "Declined", "responseStatus": {"response": "declined"},
         "start": {"dateTime": "2026-11-15T09:00:00", "timeZone": "UTC"},
         "end": {"dateTime": "2026-11-15T10:00:00", "timeZone": "UTC"}},
        {"id": "3", "subject": "Cancelled", "isCancelled": True,
         "start": {"dateTime": "2026-11-15T09:00:00", "timeZone": "UTC"},
         "end": {"dateTime": "2026-11-15T10:00:00", "timeZone": "UTC"}},
        {"id": "4", "subject": "Sync", "organizer": {"emailAddress": {"address": "o@client.com"}},
         "attendees": [{"emailAddress": {"address": "x@client.com"}}], "location": {"displayName": "Room"},
         "responseStatus": {"response": "accepted"},
         "start": {"dateTime": "2026-11-15T11:00:00", "timeZone": "UTC"},
         "end": {"dateTime": "2026-11-15T12:15:00", "timeZone": "UTC"}},
        {"id": "5", "subject": "Holiday", "isAllDay": True,
         "start": {"dateTime": "2026-11-15T00:00:00", "timeZone": "UTC"},
         "end": {"dateTime": "2026-11-16T00:00:00", "timeZone": "UTC"}},
    ]
    collector, _ = make_collector(lambda url: blocks({"value": items}))
    records = {r["subject"]: r for r in await collector.collect("events", SUBS)}
    assert set(records) == {"Spans midnight", "Sync", "Holiday"}
    assert records["Spans midnight"]["duration_minutes"] == 90
    assert records["Sync"]["duration_minutes"] == 75 and records["Sync"]["start"] == "2026-11-15T12:00:00+01:00"
    assert records["Holiday"]["duration_minutes"] is None and records["Holiday"]["is_all_day"] is True


async def test_full_windows_are_split_and_duplicates_removed(monkeypatch):
    monkeypatch.setattr(workiq_collector, "CALENDAR_CAP", 3)
    event = {"id": "same", "subject": "Long", "start": {"dateTime": "2026-11-15T01:00:00", "timeZone": "UTC"},
             "end": {"dateTime": "2026-11-15T20:00:00", "timeZone": "UTC"}}
    other = {"id": "o", "subject": "Other", "start": {"dateTime": "2026-11-15T02:00:00", "timeZone": "UTC"},
             "end": {"dateTime": "2026-11-15T03:00:00", "timeZone": "UTC"}}
    third = {"id": "t", "subject": "Third", "start": {"dateTime": "2026-11-15T04:00:00", "timeZone": "UTC"},
             "end": {"dateTime": "2026-11-15T05:00:00", "timeZone": "UTC"}}
    sizes = []

    def handler(url):
        window = sizes.append(url) or len(sizes)
        return blocks({"value": {1: [event, other, third], 2: [event, other]}.get(window, [event, third])})

    collector, calls = make_collector(handler)
    records = await collector.collect("events", SUBS)
    assert len(calls) == 3
    assert sorted(r["subject"] for r in records) == ["Long", "Other", "Third"]


async def test_incomplete_results_fail_instead_of_truncating(monkeypatch):
    monkeypatch.setattr(workiq_collector, "EMAIL_CAP", 1)
    collector, calls = make_collector(lambda url: blocks({"value": [{"subject": "x", "isDraft": False}]}))
    with pytest.raises(IncompleteCollectionError, match="received emails"):
        await collector.collect("emails", SUBS)
    assert len(calls) > 2
    collector, _ = make_collector(lambda url: blocks({"value": [], "@odata.nextLink": "https://next"}))
    with pytest.raises(IncompleteCollectionError):
        await collector.collect("events", SUBS)
    collector, _ = make_collector(lambda url: blocks({}, status=403))
    with pytest.raises(RuntimeError, match="HTTP 403"):
        await collector.collect("chats", SUBS)


async def test_chat_messages_and_limits():
    def handler(url):
        if url.startswith("/me/chats?"):
            assert "lastMessagePreview/createdDateTime%20ge%202026-11-14T23:00:00Z" in url
            return blocks({"value": [{"id": "19:abc@thread.v2", "topic": "Project X", "chatType": "group"}]})
        assert url.startswith("/me/chats/19:abc@thread.v2/messages?")
        return blocks({"value": [
            {"messageType": "message", "createdDateTime": "2026-11-15T13:00:00Z",
             "from": {"user": {"displayName": "Dana"}}, "body": {"content": "<div>Status?</div>"}},
            {"messageType": "systemEventMessage", "createdDateTime": "2026-11-15T13:01:00Z"},
            {"messageType": "message", "createdDateTime": "2026-11-15T13:02:00Z",
             "deletedDateTime": "2026-11-15T13:03:00Z", "body": {"content": "gone"}},
        ]})

    collector, _ = make_collector(handler)
    records = await collector.collect("chats", SUBS)
    assert records == [{"type": "chat_message", "chat": "Project X", "chat_type": "group",
                        "from": "Dana", "at": "2026-11-15T14:00:00+01:00", "preview": "Status?"}]
    collector, _ = make_collector(lambda url: blocks({"value": [], "@odata.nextLink": "x"}))
    with pytest.raises(IncompleteCollectionError, match="Teams chats"):
        await collector.collect("chats", SUBS)


async def test_chat_lt_windows_keep_midnight_and_dedupe_overlapping_boundaries(monkeypatch):
    from urllib.parse import parse_qs, urlsplit

    monkeypatch.setattr(workiq_collector, "CHAT_MESSAGE_CAP", 3)
    messages = [
        {"id": str(index), "createdDateTime": stamp, "body": {"content": str(index)}}
        for index, stamp in enumerate([
            "2026-11-14T22:59:59.500000Z",
            "2026-11-14T23:00:00Z",
            "2026-11-15T10:59:59.500000Z",
            "2026-11-15T11:00:00Z",
            "2026-11-15T23:00:00Z",
        ])
    ]

    def handler(url):
        if url.startswith("/me/chats?"):
            return blocks({"value": [{"id": "test", "chatType": "group"}]})
        query = parse_qs(urlsplit(url).query)
        expression = query["$filter"][0]
        parts = expression.split()
        assert parts[:2] == ["createdDateTime", "lt"] and len(parts) == 3
        upper = datetime.fromisoformat(parts[2])
        matching = [item for item in messages
                    if datetime.fromisoformat(item["createdDateTime"]) < upper]
        return blocks({"value": list(reversed(matching))[:3]})

    collector, calls = make_collector(handler)
    records = await collector.collect("chats", SUBS)
    assert [record["preview"] for record in records] == ["1", "2", "3"]
    assert records[0]["at"] == "2026-11-15T00:00:00+01:00"
    assert len(calls) > 2


async def test_chat_timestamp_ties_fail_instead_of_losing_messages(monkeypatch):
    monkeypatch.setattr(workiq_collector, "CHAT_MESSAGE_CAP", 2)

    def handler(url):
        if url.startswith("/me/chats?"):
            return blocks({"value": [{"id": "test", "chatType": "group"}]})
        return blocks({"value": [
            {"id": str(index), "createdDateTime": "2026-11-15T12:00:00.1234567Z",
             "body": {"content": "Same time"}}
            for index in range(2)
        ]})

    collector, calls = make_collector(handler)
    with pytest.raises(IncompleteCollectionError, match="share a timestamp"):
        await collector.collect("chats", SUBS)
    assert len(calls) == 3


async def test_request_budget_is_enforced():
    collector, _ = make_collector(lambda url: blocks({"value": []}))
    collector.max_requests = 1
    await collector.collect("events", SUBS)
    with pytest.raises(IncompleteCollectionError, match="Too many"):
        await collector.collect("events", SUBS)


async def test_client_routes_adapter_operations_and_disabled_send(tmp_path, monkeypatch):
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({
        "servers": {"workiq": {"transport": "http", "url": "https://workiq.svc.cloud.microsoft/mcp",
                               "auth_type": "workiq_browser"}},
        "operations": {"events": {"server": "workiq", "adapter": "workiq_graph", "resource": "events"},
                       "send_review": {"server": "workiq", "disabled": True}},
    }), encoding="utf-8")
    client = MCPClient(config)

    async def execute(server, tool, args, label, structured=False):
        assert (server, tool, label, structured) == ("workiq", "fetch", "events", True)
        return blocks({"value": []})

    monkeypatch.setattr(client, "_execute", execute)
    assert parse_records(await client.call("events", SUBS), "events") == []
    assert client.operation_disabled("send_review")
    with pytest.raises(ValueError, match="not configured"):
        await client.call("send_review", {"message": "x"})


async def test_structured_content_artifact_is_used(tmp_path, monkeypatch):
    from langchain_core.messages import ToolMessage

    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({
        "servers": {"workiq": {"transport": "http", "url": "https://workiq.svc.cloud.microsoft/mcp",
                               "auth_type": "workiq_browser"}},
        "operations": {},
    }), encoding="utf-8")
    client = MCPClient(config)
    seen = []

    class Tool:
        name = "fetch"

        async def ainvoke(self, value):
            seen.append(value)
            if isinstance(value, dict) and value.get("type") == "tool_call":
                return ToolMessage(content=[], tool_call_id=value["id"],
                                   artifact={"structured_content": {"results": [{"data": {}, "statusCode": 200}]}})
            return []

    async def tools(server):
        return [Tool()]

    monkeypatch.setattr(client, "tools", tools)
    result = await client._execute("workiq", "fetch", {"entityUrls": ["/me"]}, "probe", structured=True)
    assert result == {"results": [{"data": {}, "statusCode": 200}]}
    assert seen[0]["args"] == {"entityUrls": ["/me"]}
    assert await client._execute("workiq", "fetch", {}, "probe") == []


async def test_structured_error_artifact_does_not_bypass_failure(tmp_path, monkeypatch):
    from langchain_core.messages import ToolMessage

    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"servers": {}, "operations": {}}), encoding="utf-8")
    client = MCPClient(config)

    class Tool:
        name = "fetch"

        async def ainvoke(self, value):
            return ToolMessage(
                content="denied", status="error", tool_call_id=value["id"],
                artifact={"structured_content": {"results": []}},
            )

    async def tools(server):
        return [Tool()]

    monkeypatch.setattr(client, "tools", tools)
    with pytest.raises(RuntimeError, match="denied"):
        await client._execute("workiq", "fetch", {}, "chats", structured=True)
