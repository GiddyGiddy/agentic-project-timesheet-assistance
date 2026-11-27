import json
import ssl
import sys
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.tools import ToolException

import src.integrations.mcp_client as mcp_module
from src.integrations.mcp_client import MCPClient, parse_records
from src.integrations.mcp_client import _check_workiq_error
from langchain_mcp_adapters.interceptors import MCPToolCallRequest
from mcp.types import CallToolResult


def test_workiq_http_client_uses_verifying_system_trust_and_preserves_options(monkeypatch):
    context = ssl.create_default_context()
    options = {}
    sentinel = object()

    def make_client(**kwargs):
        options.update(kwargs)
        return sentinel

    monkeypatch.setattr(mcp_module.ssl, "create_default_context", lambda: context)
    monkeypatch.setattr(mcp_module.httpx, "AsyncClient", make_client)
    timeout = httpx.Timeout(12, read=40)
    auth = httpx.BasicAuth("synthetic-user", "synthetic-password")
    headers = {"Authorization": "Bearer synthetic-token"}

    result = mcp_module._workiq_http_client(headers=headers, timeout=timeout, auth=auth)

    assert result is sentinel
    assert options["verify"] is context
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
    assert options["headers"] == headers
    assert options["timeout"] is timeout
    assert options["auth"] is auth
    assert not options.get("follow_redirects", False)


@pytest.mark.parametrize("platform", ["win32", "linux"])
async def test_workiq_system_trust_is_scoped_to_windows_connections(tmp_path, monkeypatch, platform):
    connections = []

    class FakeClient:
        def __init__(self, servers, **kwargs):
            connections.append(servers)

        async def get_tools(self, *, server_name):
            return []

    monkeypatch.setattr(mcp_module, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(mcp_module, "MultiServerMCPClient", FakeClient)
    monkeypatch.setattr(
        mcp_module, "get_workiq_auth", lambda: SimpleNamespace(token=lambda: "synthetic-token"),
    )
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({
        "servers": {
            "workiq": {
                "transport": "http", "url": mcp_module.WORKIQ_URL,
                "auth_type": "workiq_browser", "headers": {"X-Test": "preserved"},
            },
            "other": {"transport": "http", "url": "https://example.invalid/mcp"},
        },
        "operations": {},
    }))
    client = MCPClient(config, settings=mcp_module.Settings(timesheet_data_dir=tmp_path))

    await client.tools("workiq")
    await client.tools("other")

    workiq = connections[1]["workiq"]
    assert workiq["headers"] == {"X-Test": "preserved", "Authorization": "Bearer synthetic-token"}
    if platform == "win32":
        assert workiq["httpx_client_factory"] is mcp_module._workiq_http_client
    else:
        assert "httpx_client_factory" not in workiq
    assert "httpx_client_factory" not in connections[0]["other"]


@pytest.mark.parametrize("error", [
    {"error": {"code": "BadRequest", "message": "private source text"}},
    {"code": "BadRequest", "message": "private source text"},
])
async def test_workiq_structured_only_error_preserves_safe_diagnostics(error):
    async def handler(request):
        return CallToolResult(
            content=[], isError=True,
            structuredContent={"results": [{"statusCode": 400, "error": error}]},
        )

    request = MCPToolCallRequest(name="fetch", args={}, server_name="workiq")
    with pytest.raises(RuntimeError, match="HTTP 400, BadRequest") as caught:
        await _check_workiq_error(request, handler)
    assert "private source text" not in str(caught.value)


async def test_workiq_successful_structured_response_is_unchanged():
    response = CallToolResult(
        content=[], structuredContent={"results": [{"statusCode": 200, "data": {"value": []}}]},
    )

    async def handler(request):
        return response

    request = MCPToolCallRequest(name="fetch", args={}, server_name="workiq")
    assert await _check_workiq_error(request, handler) is response


async def test_stdio_mcp_tool_and_argument_mapping(tmp_path):
    server = tmp_path / "server.py"
    server.write_text(
        'from mcp.server.fastmcp import FastMCP\n'
        'mcp = FastMCP("test")\n'
        '@mcp.tool()\n'
        'def fetch(day: str) -> list[dict[str, str]]:\n'
        '    if day == "empty":\n'
        '        return []\n'
        '    return [{"day": day}, {"day": day + "-second"}]\n'
        '@mcp.tool()\n'
        'def fail() -> str:\n'
        '    raise ValueError("delivery failed")\n'
        'if __name__ == "__main__":\n'
        '    mcp.run()\n',
        encoding="utf-8",
    )
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({
        "servers": {"test": {
            "transport": "stdio", "command": sys.executable, "args": [str(server)]
        }},
        "operations": {
            "events": {
                "server": "test", "tool": "fetch", "arguments": {"day": "{date}"}
            },
            "send_review": {"server": "test", "tool": "fail", "arguments": {}},
        },
    }), encoding="utf-8")
    client = MCPClient(config)
    result = await client.call("events", {"date": "2026-11-15"})
    assert parse_records(result, "events") == [
        {"day": "2026-11-15"}, {"day": "2026-11-15-second"}
    ]
    assert parse_records(await client.call("events", {"date": "empty"}), "events") == []
    with pytest.raises(ValueError, match="not configured"):
        await client.call("emails", {})
    with pytest.raises(ToolException):
        await client.call("send_review", {})
