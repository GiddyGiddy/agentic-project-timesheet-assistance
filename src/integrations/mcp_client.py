import json
import os
import re
import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from langchain_core.messages import ToolMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.interceptors import MCPToolCallRequest, MCPToolCallResult
from mcp.types import CallToolResult

from config.settings import Settings
from src.integrations.workiq_auth import WORKIQ_URL, get_workiq_auth
from src.integrations.workiq_collector import WorkIQCollector

TOOL_CACHE_SECONDS = 1200


_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


async def _check_workiq_error(
    request: MCPToolCallRequest,
    handler: Callable[[MCPToolCallRequest], Awaitable[MCPToolCallResult]],
) -> MCPToolCallResult:
    result = await handler(request)
    if isinstance(result, CallToolResult) and result.isError:
        details = []
        results = (result.structuredContent or {}).get("results")
        for item in results if isinstance(results, list) else []:
            if not isinstance(item, dict):
                continue
            status = item.get("statusCode")
            if isinstance(status, int):
                details.append(f"HTTP {status}")
            error = item.get("error")
            if isinstance(error, dict):
                error = error.get("error", error)
            code = error.get("code") if isinstance(error, dict) else item.get("errorCode")
            if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", code):
                details.append(code)
        if details:
            raise RuntimeError(f"Work IQ tool '{request.name}' failed ({', '.join(details)})")
    return result


def _resolve_env(value: Any, env: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _resolve_env(item, env) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_env(item, env) for item in value]
    if isinstance(value, str):
        def substitute(match: re.Match[str]) -> str:
            name = match.group(1)
            if not env.get(name):
                raise ValueError(f"Missing environment variable referenced by MCP config: {name}")
            return env[name]
        return _ENV_REFERENCE.sub(substitute, value)
    return value


def _fill_arguments(value: Any, substitutions: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _fill_arguments(item, substitutions) for key, item in value.items()}
    if isinstance(value, list):
        return [_fill_arguments(item, substitutions) for item in value]
    if isinstance(value, str):
        for key, replacement in substitutions.items():
            value = value.replace("{" + key + "}", replacement)
        if re.search(r"\{[a-z_]+\}", value):
            raise ValueError("Unresolved MCP argument placeholder")
    return value


class MCPClient:
    def __init__(self, path: Path, *, settings: Settings | None = None):
        config = json.loads(path.read_text(encoding="utf-8"))
        settings = settings or Settings()
        dotenv = dotenv_values(".env") if settings.timesheet_data_dir is None else {}
        env = {key: value for key, value in dotenv.items() if value is not None}
        env.update(os.environ)
        self.servers = _resolve_env(config["servers"], env)
        self.operations = config["operations"]
        self.workiq_servers = set()
        self._tool_cache: dict[str, tuple[float, list]] = {}
        for name, server in self.servers.items():
            auth = server.pop("auth_type", None)
            if auth == "workiq_browser":
                if server.get("url") != WORKIQ_URL or server.get("transport") != "http":
                    raise ValueError("Work IQ authentication requires the official HTTPS MCP endpoint")
                if any(key.lower() == "authorization" for key in server.get("headers", {})):
                    raise ValueError("Work IQ authorization header is managed by the sign-in flow")
                self.workiq_servers.add(name)
            elif auth is not None:
                raise ValueError(f"Unsupported MCP authentication type: {auth}")
        self.client = MultiServerMCPClient(self.servers, handle_tool_errors=False)

    async def tools(self, server: str):
        if server not in self.servers:
            raise ValueError(f"MCP server '{server}' is not configured")
        if server not in self.workiq_servers:
            return await self.client.get_tools(server_name=server)
        cached = self._tool_cache.get(server)
        if cached and time.monotonic() - cached[0] < TOOL_CACHE_SECONDS:
            return cached[1]
        token = await asyncio.to_thread(lambda: get_workiq_auth().token())
        connection = dict(self.servers[server])
        connection["headers"] = {**connection.get("headers", {}), "Authorization": f"Bearer {token}"}
        client = MultiServerMCPClient(
            {server: connection}, handle_tool_errors=False,
            tool_interceptors=[_check_workiq_error],
        )
        tools = await client.get_tools(server_name=server)
        self._tool_cache[server] = (time.monotonic(), tools)
        return tools

    def operation_disabled(self, operation: str) -> bool:
        return bool(self.operations.get(operation, {}).get("disabled"))

    async def _execute(
        self, server: str, tool_name: str, args: dict[str, Any], label: str, structured: bool = False
    ) -> Any:
        tools = await self.tools(server)
        matches = [tool for tool in tools if tool.name == tool_name]
        if len(matches) != 1:
            raise ValueError(f"MCP tool for '{label}' was not found uniquely")
        if structured:
            result = await matches[0].ainvoke(
                {"type": "tool_call", "id": f"{label}-call", "name": tool_name, "args": args}
            )
            if isinstance(result, ToolMessage) and result.status == "error":
                raise RuntimeError(f"MCP operation '{label}' failed: {result.content}")
            artifact = getattr(result, "artifact", None)
            if isinstance(artifact, dict) and artifact.get("structured_content") is not None:
                return artifact["structured_content"]
        else:
            result = await matches[0].ainvoke(args)
        if isinstance(result, ToolMessage):
            if result.status == "error":
                raise RuntimeError(f"MCP operation '{label}' failed: {result.content}")
            result = result.content
        return result

    async def call(self, operation: str, substitutions: dict[str, str]) -> Any:
        definition = self.operations.get(operation)
        if not definition or definition.get("disabled") or not definition.get("server"):
            raise ValueError(f"MCP operation '{operation}' is not configured")
        server = definition["server"]
        if server not in self.servers:
            raise ValueError(f"MCP server '{server}' is not configured")
        if definition.get("adapter"):
            if definition["adapter"] != "workiq_graph" or server not in self.workiq_servers:
                raise ValueError(f"MCP adapter for '{operation}' is not supported")
            collector = WorkIQCollector(
                lambda tool, args: self._execute(server, tool, args, operation, structured=True)
            )
            return await collector.collect(definition.get("resource", ""), substitutions)
        if not definition.get("tool"):
            raise ValueError(f"MCP operation '{operation}' is not configured")
        args = _fill_arguments(definition.get("arguments", {}), substitutions)
        return await self._execute(server, definition["tool"], args, operation)


def parse_records(result: Any, operation: str) -> list[dict[str, Any]]:
    if isinstance(result, str):
        result = json.loads(result)
        if isinstance(result, dict):
            result = [result]
    elif isinstance(result, list) and all(
        isinstance(block, dict) and block.get("type") == "text" and "text" in block
        for block in result
    ) and result:
        records = []
        for block in result:
            parsed = json.loads(block["text"])
            records.extend(parsed if isinstance(parsed, list) else [parsed])
        result = records
    if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
        raise ValueError(f"MCP operation '{operation}' must return a JSON array of objects")
    return result
