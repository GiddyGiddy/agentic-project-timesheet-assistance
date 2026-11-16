from collections import defaultdict
from pathlib import Path

from src.database.repository import Repository
from src.graph.state import Registry
from src.integrations.excel_writer import write_timesheet


def export_month(
    repository: Repository,
    registry: Registry,
    template: Path,
    output_dir: Path,
    year: int,
    month: int,
) -> Path:
    entries = repository.monthly_entries(year, month)
    if not entries:
        raise ValueError("No approved entries found for this month")
    totals: dict[tuple[str, int], float] = defaultdict(float)
    for entry in entries:
        totals[entry.project_code, entry.entry_date.day] += entry.hours
    output = output_dir / f"timesheet-{year:04d}-{month:02d}.xlsx"
    write_timesheet(
        template, output, year, month, dict(totals),
        {project.code for project in registry.projects}, registry.excel,
    )
    return output
