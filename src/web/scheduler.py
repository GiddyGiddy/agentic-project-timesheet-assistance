import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config.settings import Settings
from src.main import checked_export, create_scheduler, graph_action, load_registry

logger = logging.getLogger(__name__)


class DashboardScheduler:
    def __init__(self, workflow_lock: asyncio.Lock):
        self.lock = workflow_lock
        self.control_lock = asyncio.Lock()
        self.scheduler: AsyncIOScheduler | None = None
        self.timezone: str | None = None
        self.last_runs: dict[str, dict[str, str]] = {}
        self.closed = False

    @property
    def running(self) -> bool:
        return self.scheduler is not None and self.scheduler.running

    async def start(self) -> None:
        async with self.control_lock:
            async with self.lock:
                if self.closed:
                    raise ValueError("Application is shutting down")
                if self.running:
                    raise ValueError("Dashboard scheduler is already running")
                timezone = Settings().timezone
                self.timezone = timezone
                self.scheduler = create_scheduler(
                    timezone, self._daily_job, self._export_job
                )
                self.scheduler.start()

    async def stop(self) -> None:
        async with self.control_lock:
            if not self.running or self.scheduler is None:
                raise ValueError("Dashboard scheduler is not running")
            scheduler = self.scheduler
            scheduler.pause()
            async with self.lock:
                scheduler.shutdown(wait=False)
                await asyncio.sleep(0)
                self.scheduler = None
                self.timezone = None

    async def close(self) -> None:
        async with self.control_lock:
            self.closed = True
            if self.running and self.scheduler is not None:
                self.scheduler.pause()
            async with self.lock:
                if self.running and self.scheduler is not None:
                    self.scheduler.shutdown(wait=False)
                    await asyncio.sleep(0)
                self.scheduler = None
                self.timezone = None

    def status(self) -> dict:
        jobs = {}
        if self.running and self.scheduler is not None:
            for job in self.scheduler.get_jobs():
                jobs[job.id] = job.next_run_time.isoformat() if job.next_run_time else None
        return {
            "running": self.running,
            "timezone": self.timezone,
            "next_runs": jobs,
            "last_runs": self.last_runs,
        }

    async def _run(self, job: str) -> None:
        async with self.lock:
            if self.closed:
                logger.info("Skipping scheduled %s during application shutdown", job)
                return
            try:
                settings = Settings()
                registry = load_registry(settings.projects_registry_path)
                today = datetime.now(ZoneInfo(self.timezone or settings.timezone)).date()
                if job == "daily":
                    await graph_action(settings, registry, today, "daily", verbose=False)
                    message = "Proposal generated; review is required."
                else:
                    output = await checked_export(settings, registry, today.year, today.month)
                    message = f"Generated {output.name}."
                self.last_runs[job] = {
                    "status": "succeeded", "message": message,
                    "at": datetime.now(ZoneInfo(settings.timezone)).isoformat(),
                }
            except Exception as exc:
                logger.error("Scheduled %s job failed (%s)", job, type(exc).__name__)
                if isinstance(exc, ValueError):
                    message = str(exc)
                elif isinstance(exc, FileExistsError):
                    message = "Export file already exists; it was not overwritten."
                elif isinstance(exc, FileNotFoundError):
                    message = "A required configuration or template file was not found."
                else:
                    message = f"Job failed ({type(exc).__name__}); check the server console."
                self.last_runs[job] = {
                    "status": "failed", "message": message,
                    "at": datetime.now().astimezone().isoformat(),
                }

    async def _daily_job(self) -> None:
        await self._run("daily")

    async def _export_job(self) -> None:
        await self._run("month-end")
