import asyncio
import calendar
import hmac
import json
import logging
import os
import re
import secrets
import tempfile
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Literal
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from config.settings import Settings
from src.database.repository import Repository
from src.graph.state import Entry, Registry
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


def create_app(token: str) -> FastAPI:
    workflow_lock = asyncio.Lock()
    scheduler = DashboardScheduler(workflow_lock)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        if scheduler.running:
            await scheduler.stop()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if (
            request.client is None
            or request.client.host not in ("127.0.0.1", "::1")
            or request.headers.get("host") != f"{HOST}:{PORT}"
        ):
            return JSONResponse({"detail": "Local access only"}, status_code=403)
        if request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            if origin and origin != f"http://{HOST}:{PORT}":
                return JSONResponse({"detail": "Invalid origin"}, status_code=403)
            supplied = request.headers.get("x-timesheet-token", "")
            if not hmac.compare_digest(supplied, token):
                return JSONResponse({"detail": "Invalid dashboard token"}, status_code=403)
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

    @app.get("/api/setup")
    def read_setup():
        settings = Settings()
        config = json.loads(settings.mcp_config_path.read_text(encoding="utf-8"))
        operations = config.get("operations", {})
        servers = config.get("servers", {})
        return {
            "mcp_operations": {
                name: bool(
                    not operations.get(name, {}).get("disabled")
                    and (operations.get(name, {}).get("tool") or operations.get(name, {}).get("adapter"))
                    and operations.get(name, {}).get("server") in servers
                )
                for name in ("emails", "events", "chats", "send_review")
            },
        }

    @app.post("/api/workiq/sign-in")
    async def workiq_sign_in():
        try:
            auth = await asyncio.to_thread(get_workiq_auth)
            await asyncio.to_thread(auth.sign_in)
            tools = await MCPClient(Settings().mcp_config_path).tools("workiq")
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
            output = settings.output_dir / f"timesheet-{year:04d}-{month_number:02d}.xlsx"
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
                "output_exists": output.is_file(),
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
                raise HTTPException(409, "An export already exists; it was not overwritten") from exc
            except (ValueError, FileNotFoundError) as exc:
                raise HTTPException(400, str(exc)) from exc
            except OSError as exc:
                LOGGER.error("Excel export failed (%s)", type(exc).__name__)
                raise HTTPException(500, "Could not write export; check file permissions.") from exc
            return {"filename": path.name}

    @app.get("/api/monthly/{month}/download")
    async def monthly_download(month: str):
        year, month_number = month_parts(month)
        path = Settings().output_dir / f"timesheet-{year:04d}-{month_number:02d}.xlsx"
        if not path.is_file():
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
    uvicorn.run(create_app(token), host=HOST, port=PORT, access_log=False)


if __name__ == "__main__":
    main()
