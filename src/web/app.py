import asyncio
import calendar
import hmac
import io
import json
import logging
import os
import re
import secrets
import tempfile
from collections import defaultdict
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException
from pydantic import BaseModel, ConfigDict, ValidationError

from config.settings import Settings
from config.storage import initialize_user_storage
from config.user_settings import (
    SETTINGS_FILE, UserConfiguration, read_user_configuration, write_user_configuration,
)
from src.agents.excel_agent import latest_monthly_output
from src.database.repository import Repository
from src.graph.state import Entry, Registry
from src.integrations.excel_writer import template_for_layout
from src.main import checked_export, graph_action, load_registry, pending_workflows
from src.integrations.mcp_client import MCPClient
from src.integrations.workiq_auth import get_workiq_auth
from src.web.scheduler import DashboardScheduler

LOGGER = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"
HOST = "127.0.0.1"
PORT = 8765


class DailyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[Entry] | None = None
    replace: bool = False


class SetupInput(UserConfiguration):
    clear_api_key: bool = False


async def _limited_body(request: Request, maximum: int) -> bytes:
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > maximum:
            raise HTTPException(413, "Upload exceeds the allowed size")
        content.extend(chunk)
    return bytes(content)


def _validate_template(content: bytes) -> None:
    try:
        with ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if (
                len(entries) > 1000 or sum(item.file_size for item in entries) > 50 * 1024 * 1024
                or any(item.filename.lower().endswith("vbaproject.bin") for item in entries)
                or any(item.flag_bits & 1 or item.compress_type not in (0, 8) for item in entries)
            ):
                raise ValueError("Unsupported workbook contents")
            types = ElementTree.fromstring(archive.read("[Content_Types].xml"))
            workbook_types = [
                item.attrib.get("ContentType", "") for item in types
                if item.attrib.get("PartName") == "/xl/workbook.xml"
            ]
            if workbook_types != ["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"]:
                raise ValueError("Only macro-free XLSX workbooks are supported")
        workbook = load_workbook(io.BytesIO(content), read_only=True, keep_links=False)
        try:
            if not workbook.sheetnames:
                raise ValueError("A template must contain at least one worksheet")
        finally:
            workbook.close()
    except (BadZipFile, InvalidFileException, ValueError, KeyError, OSError, SyntaxError) as exc:
        raise HTTPException(422, "Choose a valid, macro-free .xlsx template (expanded size at most 50 MB)") from exc


def _atomic_registry_write(path: Path, registry: Registry) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".registry-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(registry.model_dump_json(indent=2, exclude_none=True) + "\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def create_app(
    token: str, *, port: int = PORT, on_shutdown: Callable[[], None] | None = None,
) -> FastAPI:
    workflow_lock = asyncio.Lock()
    scheduler = DashboardScheduler(workflow_lock)
    shutdown_task: asyncio.Task[None] | None = None
    shutdown_failed = False

    def report_shutdown(task: asyncio.Task[None]) -> None:
        nonlocal shutdown_failed
        if not task.cancelled() and (error := task.exception()) is not None:
            shutdown_failed = True
            LOGGER.error("Clean shutdown failed (%s)", type(error).__name__)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        initialize_user_storage(Settings())
        try:
            yield
        finally:
            if shutdown_task is not None:
                await shutdown_task
            else:
                await scheduler.close()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if (
            request.client is None
            or request.client.host not in ("127.0.0.1", "::1")
            or request.headers.get("host") != f"{HOST}:{port}"
        ):
            return JSONResponse({"detail": "Local access only"}, status_code=403)
        if request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            if origin and origin != f"http://{HOST}:{port}":
                return JSONResponse({"detail": "Invalid origin"}, status_code=403)
            supplied = request.headers.get("x-timesheet-token", "")
            if not hmac.compare_digest(supplied, token):
                return JSONResponse({"detail": "Invalid dashboard token"}, status_code=403)
            if shutdown_task is not None and request.url.path != "/api/lifecycle":
                return JSONResponse({"detail": "Application is shutting down"}, status_code=409)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        return response

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/api/lifecycle")
    async def lifecycle():
        return {
            "exit_available": on_shutdown is not None,
            "stopping": shutdown_task is not None,
            "shutdown_failed": shutdown_failed,
        }

    @app.post("/api/lifecycle")
    async def exit_application():
        nonlocal shutdown_task
        if on_shutdown is None:
            raise HTTPException(405, "This server does not support application exit")
        if shutdown_task is None:
            async def drain_and_exit():
                await scheduler.close()
                on_shutdown()

            shutdown_task = asyncio.create_task(drain_and_exit())
            shutdown_task.add_done_callback(report_shutdown)
        return JSONResponse({"stopping": True}, status_code=202)

    @app.get("/api/setup")
    def read_setup():
        settings = Settings()
        config = json.loads(settings.mcp_config_path.read_text(encoding="utf-8"))
        operations = config.get("operations", {})
        servers = config.get("servers", {})
        result: dict[str, object] = {
            "mcp_operations": {
                name: bool(
                    not operations.get(name, {}).get("disabled")
                    and (operations.get(name, {}).get("tool") or operations.get(name, {}).get("adapter"))
                    and operations.get(name, {}).get("server") in servers
                )
                for name in ("emails", "events", "chats", "send_review")
            },
        }
        if settings.timesheet_data_dir is not None:
            public_fields = (
                "azure_openai_endpoint", "azure_openai_deployment_name", "azure_openai_api_version",
                "tenant_id", "client_id", "timezone",
            )
            services_configured = all((
                settings.azure_openai_endpoint, settings.azure_openai_deployment_name,
                settings.azure_openai_api_key, settings.tenant_id, settings.client_id,
            ))
            template = template_for_layout(
                settings.template_path, load_registry(settings.projects_registry_path).excel
            )
            result["profile_setup"] = {
                **{field: getattr(settings, field) for field in public_fields},
                "api_key_configured": bool(settings.azure_openai_api_key),
                "services_configured": services_configured,
                "template_available": template.is_file(),
                "settings_saved": (settings.timesheet_data_dir / "config" / SETTINGS_FILE).is_file(),
                "data_directory": str(settings.timesheet_data_dir),
                "environment_overrides": [
                    field for field in (*public_fields, "azure_openai_api_key")
                    if field.casefold() in {key.casefold() for key in os.environ}
                ],
            }
        return result

    @app.put("/api/setup")
    async def save_setup(request: Request):
        async with workflow_lock:
            settings = Settings()
            root = settings.timesheet_data_dir
            if root is None:
                raise HTTPException(405, "Repository development uses environment variables or .env")
            if scheduler.running:
                raise HTTPException(409, "Stop the dashboard scheduler before changing service settings")
            try:
                data = SetupInput.model_validate(json.loads(await _limited_body(request, 16384)))
            except (ValidationError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise HTTPException(422, "Invalid settings. Check the HTTPS resource root, deployment, IDs and time zone.") from exc
            if data.clear_api_key and data.azure_openai_api_key:
                raise HTTPException(422, "Choose either a new API key or clear the saved key, not both")
            environment = {key.casefold() for key in os.environ}
            overrides = {
                field: getattr(settings, field) for field in UserConfiguration.model_fields
                if field in environment
            }
            if "azure_openai_api_key" in overrides and (data.azure_openai_api_key or data.clear_api_key):
                raise HTTPException(409, "The API key is controlled by an environment variable; remove that override first")
            try:
                effective = UserConfiguration.model_validate({
                    **data.model_dump(exclude={"clear_api_key"}), **overrides,
                })
            except ValidationError as exc:
                raise HTTPException(409, "Correct or remove invalid service environment overrides before saving.") from exc
            if any(
                field != "azure_openai_api_key" and getattr(data, field) != getattr(effective, field)
                for field in overrides
            ):
                raise HTTPException(409, "Remove environment overrides before editing their read-only fields.")
            try:
                previous = await asyncio.to_thread(read_user_configuration, root)
                key = data.azure_openai_api_key or (previous.azure_openai_api_key if previous else None)
                values = data.model_dump(exclude={"clear_api_key"})
                values["azure_openai_api_key"] = None if data.clear_api_key else key
                configuration = UserConfiguration.model_validate(values)
                await asyncio.to_thread(write_user_configuration, root, configuration)
            except (OSError, ValueError) as exc:
                LOGGER.error("Saving protected service settings failed (%s)", type(exc).__name__)
                raise HTTPException(500, "Could not save Windows-protected settings. Existing settings were not replaced.") from exc
            return {"saved": True}

    @app.post("/api/setup/template")
    async def upload_template(request: Request):
        async with workflow_lock:
            settings = Settings()
            if settings.timesheet_data_dir is None:
                raise HTTPException(405, "Template upload is available only in per-user profiles")
            if scheduler.running:
                raise HTTPException(409, "Stop the dashboard scheduler before uploading a template")
            template = template_for_layout(
                settings.template_path, load_registry(settings.projects_registry_path).excel
            )
            if template.exists():
                raise HTTPException(409, "A template already exists and was not overwritten")
            content = await _limited_body(request, 10 * 1024 * 1024)
            await asyncio.to_thread(_validate_template, content)
            created = False
            try:
                template.parent.mkdir(parents=True, exist_ok=True)
                with template.open("xb") as stream:
                    created = True
                    stream.write(content)
            except FileExistsError as exc:
                raise HTTPException(409, "A template already exists and was not overwritten") from exc
            except OSError as exc:
                if created:
                    template.unlink(missing_ok=True)
                LOGGER.error("Template upload failed (%s)", type(exc).__name__)
                raise HTTPException(500, "Could not save the Excel template; check disk space and permissions.") from exc
            return {"saved": True}

    @app.post("/api/workiq/sign-in")
    async def workiq_sign_in():
        async with workflow_lock:
            try:
                auth = await asyncio.to_thread(get_workiq_auth)
                await asyncio.to_thread(auth.sign_in)
                settings = Settings()
                tools = await MCPClient(settings.mcp_config_path, settings=settings).tools("workiq")
                return {"connected": True, "tools": sorted(tool.name for tool in tools)}
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            except Exception as exc:
                LOGGER.error("Work IQ connection check failed (%s)", type(exc).__name__)
                raise HTTPException(
                    502, "Sign-in or Work IQ connection check failed; check server logs."
                ) from exc

    @app.get("/api/registry")
    def read_registry():
        return load_registry(Settings().projects_registry_path)

    @app.put("/api/registry")
    async def save_registry(registry: Registry):
        async with workflow_lock:
            _atomic_registry_write(Settings().projects_registry_path, registry)
        return {"saved": True}

    def month_parts(month: str) -> tuple[int, int]:
        if not re.fullmatch(r"\d{4}-\d{2}", month):
            raise HTTPException(422, "Month must be YYYY-MM")
        try:
            day = date.fromisoformat(month + "-01")
        except ValueError as exc:
            raise HTTPException(422, "Invalid month") from exc
        return day.year, day.month

    @app.get("/api/monthly/{month}")
    async def monthly_overview(month: str):
        year, month_number = month_parts(month)
        async with workflow_lock:
            settings = Settings()
            registry = load_registry(settings.projects_registry_path)
            repository = Repository(settings.database_url)
            entries = repository.monthly_entries(year, month_number)
            totals: dict[str, float] = defaultdict(float)
            days = set()
            for entry in entries:
                totals[entry.project_code] += entry.hours
                days.add(entry.entry_date.day)
            pending = await pending_workflows(settings, registry, repository, year, month_number)
            output = latest_monthly_output(settings.output_dir, year, month_number, registry.excel)
            return {
                "month": month,
                "projects": [{"project_code": code, "hours": hours}
                             for code, hours in sorted(totals.items())],
                "total_hours": sum(totals.values()),
                "days_with_hours": len(days),
                "days_without_hours": [
                    day for day in range(1, calendar.monthrange(year, month_number)[1] + 1)
                    if day not in days
                ],
                "pending_dates": pending,
                "output_exists": output is not None,
            }

    @app.post("/api/monthly/{month}/export")
    async def monthly_export(month: str):
        year, month_number = month_parts(month)
        async with workflow_lock:
            try:
                settings = Settings()
                path = await checked_export(
                    settings, load_registry(settings.projects_registry_path), year, month_number
                )
            except FileExistsError as exc:
                raise HTTPException(409, "Could not create a new export; please retry.") from exc
            except (ValueError, FileNotFoundError) as exc:
                raise HTTPException(400, str(exc)) from exc
            except OSError as exc:
                LOGGER.error("Excel export failed (%s)", type(exc).__name__)
                raise HTTPException(500, "Could not write export; check file permissions.") from exc
            return {"filename": path.name}

    @app.get("/api/monthly/{month}/download")
    async def monthly_download(month: str):
        year, month_number = month_parts(month)
        async with workflow_lock:
            settings = Settings()
            registry = load_registry(settings.projects_registry_path)
            path = latest_monthly_output(settings.output_dir, year, month_number, registry.excel)
            if path is None:
                raise HTTPException(404, "No export exists for this month")
            return FileResponse(
                path, filename=path.name,
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

    @app.get("/api/scheduler")
    async def scheduler_status():
        return scheduler.status()

    @app.post("/api/scheduler/start")
    async def scheduler_start():
        try:
            await scheduler.start()
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return scheduler.status()

    @app.post("/api/scheduler/stop")
    async def scheduler_stop():
        try:
            await scheduler.stop()
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return scheduler.status()

    @app.get("/api/daily/{day}")
    async def show_day(day: date):
        return await run_action(day, "show", DailyInput())

    @app.post("/api/daily/{day}/{action}")
    async def daily_action(
        day: date, action: Literal["daily", "approve", "reject", "retry"], data: DailyInput
    ):
        return await run_action(day, action, data)

    async def run_action(day: date, action: str, data: DailyInput):
        if data.entries is not None and action != "approve":
            raise HTTPException(422, "Entries can only be supplied when approving")
        if data.replace and action != "daily":
            raise HTTPException(422, "Replace is only valid for a daily run")
        async with workflow_lock:
            try:
                settings = Settings()
                registry = load_registry(settings.projects_registry_path)
                entries = (
                    [entry.model_dump() for entry in data.entries]
                    if data.entries is not None else None
                )
                return await graph_action(
                    settings, registry, day, action, entries, replace=data.replace,
                    verbose=False,
                )
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            except Exception as exc:
                LOGGER.error("Daily workflow failed (%s)", type(exc).__name__)
                raise HTTPException(
                    500, "Workflow failed. Check MCP and Azure configuration and server logs."
                ) from exc

    return app


def main() -> None:
    token = secrets.token_urlsafe(32)
    print(f"Open http://{HOST}:{PORT}/#token={token}", flush=True)
    print("Local use only. Do not share the dashboard URL.", flush=True)
    def request_shutdown() -> None:
        server.should_exit = True

    server = uvicorn.Server(uvicorn.Config(
        create_app(token, on_shutdown=request_shutdown), host=HOST, port=PORT, access_log=False,
    ))
    server.run()


if __name__ == "__main__":
    main()
