import json
import sys

import pytest
from langchain_core.tools import ToolException

from src.integrations.mcp_client import MCPClient, parse_records
from src.integrations.mcp_client import _check_workiq_error
from langchain_mcp_adapters.interceptors import MCPToolCallRequest
from mcp.types import CallToolResult


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
