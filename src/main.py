import argparse
import asyncio
import calendar
import json
import logging
import os
import sqlite3
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from config.settings import Settings, default_user_data_dir
from config.storage import import_user_data, initialize_user_storage
from src.agents.excel_agent import export_month
from src.agents.mapper_agent import validate_entries
from src.database.repository import Repository
from src.graph.state import Registry
from src.graph.workflow import build_workflow
from src.integrations.mcp_client import MCPClient
from src.integrations.workiq_auth import sign_in_cli

logger = logging.getLogger(__name__)


def load_registry(path: Path) -> Registry:
    return Registry.model_validate_json(path.read_text(encoding="utf-8"))


def graph_config(day: date) -> dict:
    return {"configurable": {"thread_id": f"daily:{day.isoformat()}"}}


async def graph_action(
    settings: Settings,
    registry: Registry,
    day: date,
    action: str,
    corrected: list[dict] | None = None,
    replace: bool = False,
    verbose: bool = True,
) -> dict:
    settings.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    repository = Repository(settings.database_url)
    async with AsyncSqliteSaver.from_conn_string(str(settings.checkpoint_path)) as saver:
        graph = build_workflow(settings, registry, repository, saver)
        config = graph_config(day)
        snapshot = await graph.aget_state(config)
        if action == "daily":
            if snapshot.next:
                raise ValueError("This date has an unfinished run; inspect or resume it first")
            if snapshot.values and not replace:
                raise ValueError("A run already exists for this date; use --replace to start again")
            result = await graph.ainvoke(
                {
                    "target_date": day.isoformat(),
                    "raw_emails": [],
                    "raw_events": [],
                    "raw_chats": [],
                    "mapped_entries": [],
                    "approval_status": None,
                },
                config,
            )
            if verbose:
                print(json.dumps(result.get("__interrupt__", []), default=str, ensure_ascii=False))
                print(f"Review pending for {day.isoformat()}; use 'show' and 'approve' or 'reject'.")
        elif action == "retry":
            if not snapshot.next or "review" in snapshot.next:
                raise ValueError("No failed, non-review workflow step is available to retry")
            await graph.ainvoke(None, config)
            if verbose:
                print(f"Workflow resumed for {day.isoformat()}; use 'show' to inspect it.")
        elif action == "show":
            if not snapshot.values:
                raise ValueError("No workflow found for this date")
            if verbose:
                print(json.dumps({
                    "date": day.isoformat(),
                    "pending_nodes": snapshot.next,
                    "entries": snapshot.values.get("mapped_entries", []),
                    "approval_status": snapshot.values.get("approval_status"),
                }, indent=2, ensure_ascii=False))
        else:
            if "review" not in snapshot.next:
                raise ValueError("No approval is pending for this date")
            decision = {"status": "approved" if action == "approve" else "rejected"}
            if corrected is not None:
                if action != "approve":
                    raise ValueError("Corrected entries are only allowed for approval")
                decision["entries"] = [
                    entry.model_dump() for entry in validate_entries(corrected, registry)
                ]
            await graph.ainvoke(Command(resume=decision), config)
            if verbose:
                print(f"{day.isoformat()}: {decision['status']}")
        snapshot = await graph.aget_state(config)
        return {
            "date": day.isoformat(),
            "pending_nodes": list(snapshot.next),
            "entries": snapshot.values.get("mapped_entries", []),
            "approval_status": snapshot.values.get("approval_status"),
        }


def parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use a date in YYYY-MM-DD format") from exc


def parse_month(value: str) -> tuple[int, int]:
    try:
        parsed = datetime.strptime(value, "%Y-%m")
        return parsed.year, parsed.month
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use a month in YYYY-MM format") from exc


async def pending_workflows(
    settings: Settings, registry: Registry, repository: Repository, year: int, month: int
) -> list[str]:
    pending = []
    if settings.checkpoint_path.exists():
        async with AsyncSqliteSaver.from_conn_string(str(settings.checkpoint_path)) as saver:
            graph = build_workflow(settings, registry, repository, saver)
            for day in range(1, calendar.monthrange(year, month)[1] + 1):
                current = date(year, month, day)
                snapshot = await graph.aget_state(graph_config(current))
                if snapshot.next:
                    pending.append(current.isoformat())
    return pending


async def checked_export(settings: Settings, registry: Registry, year: int, month: int) -> Path:
    repository = Repository(settings.database_url)
    pending = await pending_workflows(settings, registry, repository, year, month)
    if pending:
        raise ValueError(f"Unfinished daily workflows prevent export: {', '.join(pending)}")
    return export_month(
        repository, registry, settings.template_path, settings.output_dir, year, month
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Human-approved project timesheets")
    commands = result.add_subparsers(dest="command", required=True)
    daily = commands.add_parser("daily", help="Collect and propose a daily timesheet")
    daily.add_argument("--date", type=parse_day, required=True)
    daily.add_argument("--replace", action="store_true")
    daily.add_argument("--workiq-sign-in", action="store_true")
    show = commands.add_parser("show", help="Show a daily proposal")
    show.add_argument("--date", type=parse_day, required=True)
    retry = commands.add_parser("retry", help="Retry a failed workflow step")
    retry.add_argument("--date", type=parse_day, required=True)
    retry.add_argument("--workiq-sign-in", action="store_true")
    approve = commands.add_parser("approve", help="Approve a proposal")
    approve.add_argument("--date", type=parse_day, required=True)
    approve.add_argument("--entries", type=Path, help="JSON file containing corrected entries")
    reject = commands.add_parser("reject", help="Reject a proposal")
    reject.add_argument("--date", type=parse_day, required=True)
    export = commands.add_parser("export", help="Export approved entries for a month")
    export.add_argument("--month", type=parse_month, required=True)
    schedule = commands.add_parser("schedule", help="Run the local 18:00 scheduler")
    schedule.add_argument("--workiq-sign-in", action="store_true")
    commands.add_parser("workiq-check", help="Sign in and list Work IQ MCP tools")
    importer = commands.add_parser("import-data", help="Import a stopped application's data into a new profile")
    importer.add_argument("--source", type=Path, required=True, help="Existing application data root")
    importer.add_argument("--destination", type=Path, help="New, absolute per-user data directory")
    importer.add_argument(
        "--source-closed", action="store_true", required=True,
        help="Confirm the source application and its scheduler are stopped",
    )
    return result


def create_scheduler(timezone: str, daily_job, export_job) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=ZoneInfo(timezone))
    scheduler.add_job(daily_job, "cron", hour=18, minute=0, id="daily")
    scheduler.add_job(export_job, "cron", day="last", hour=18, minute=30, id="month-end")
    return scheduler


async def run_scheduler(settings: Settings, registry: Registry) -> None:
    async def daily_job():
        today = datetime.now(ZoneInfo(settings.timezone)).date()
        await graph_action(settings, registry, today, "daily")

    async def export_job():
        today = datetime.now(ZoneInfo(settings.timezone)).date()
        output = await checked_export(settings, registry, today.year, today.month)
        logger.info("Exported %s", output)

    scheduler = create_scheduler(settings.timezone, daily_job, export_job)
    scheduler.start()
    logger.info("Scheduler running in %s", settings.timezone)
    try:
        await asyncio.Event().wait()
    finally:
        scheduler.shutdown(wait=False)


async def check_workiq(settings: Settings) -> None:
    await sign_in_cli()
    tools = await MCPClient(settings.mcp_config_path, settings=settings).tools("workiq")
    print("Work IQ tools:", ", ".join(sorted(tool.name for tool in tools)))


async def run_with_sign_in(coroutine):
    await sign_in_cli()
    return await coroutine()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = parser().parse_args()
    try:
        if args.command == "import-data":
            destination = args.destination
            if destination is None:
                configured = os.environ.get("TIMESHEET_DATA_DIR")
                destination = Path(configured) if configured is not None else default_user_data_dir()
            print(f"Imported data into {import_user_data(args.source, destination)}")
            return
        settings = Settings()
        initialize_user_storage(settings)
        registry = load_registry(settings.projects_registry_path)
        if args.command == "workiq-check":
            asyncio.run(check_workiq(settings))
        elif args.command == "schedule":
            if args.workiq_sign_in:
                asyncio.run(run_with_sign_in(lambda: run_scheduler(settings, registry)))
            else:
                asyncio.run(run_scheduler(settings, registry))
        elif args.command == "export":
            year, month = args.month
            print(asyncio.run(checked_export(settings, registry, year, month)))
        else:
            corrected = None
            if args.command == "approve" and args.entries:
                corrected = json.loads(args.entries.read_text(encoding="utf-8"))
                if not isinstance(corrected, list):
                    raise ValueError("Corrected entries file must contain a JSON array")
            action = lambda: graph_action(
                settings, registry, args.date, args.command, corrected,
                getattr(args, "replace", False),
            )
            if getattr(args, "workiq_sign_in", False):
                asyncio.run(run_with_sign_in(action))
            else:
                asyncio.run(action())
    except (ValueError, OSError, sqlite3.Error) as exc:
        raise SystemExit(f"Error: {exc}") from exc


if __name__ == "__main__":
    main()
