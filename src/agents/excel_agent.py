from collections import defaultdict
from pathlib import Path
import re

from src.database.repository import Repository
from src.graph.state import ExcelLayout, Registry
from src.integrations.excel_writer import template_for_layout, write_row_timesheet, write_timesheet


def monthly_output_path(output_dir: Path, year: int, month: int, layout: ExcelLayout) -> Path:
    suffix = "-rowwise" if layout.layout_mode == "rows" else ""
    return output_dir / f"timesheet-{year:04d}-{month:02d}{suffix}.xlsx"


def _latest_monthly_output(base: Path) -> tuple[Path | None, int]:
    latest = base if base.is_file() else None
    version = 1 if latest else 0
    for candidate in base.parent.glob(f"{base.stem}-*{base.suffix}"):
        suffix = candidate.name[len(base.stem) + 1:-len(base.suffix)]
        if re.fullmatch(r"[1-9][0-9]*", suffix) and candidate.is_file():
            number = int(suffix)
            if number >= 2 and number > version:
                latest, version = candidate, number
    return latest, version


def latest_monthly_output(
    output_dir: Path, year: int, month: int, layout: ExcelLayout
) -> Path | None:
    return _latest_monthly_output(monthly_output_path(output_dir, year, month, layout))[0]


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
    base = monthly_output_path(output_dir, year, month, registry.excel)
    _, version = _latest_monthly_output(base)
    template = template_for_layout(template, registry.excel)
    while True:
        output = base if version == 0 else base.with_name(f"{base.stem}-{version + 1}{base.suffix}")
        try:
            if registry.excel.layout_mode == "rows":
                write_row_timesheet(template, output, year, month, dict(totals), registry.projects, registry.excel)
            else:
                write_timesheet(
                    template, output, year, month, dict(totals),
                    {project.code for project in registry.projects}, registry.excel,
                )
            return output
        except FileExistsError:
            if not output.exists():
                raise
            version = 1 if version == 0 else version + 1
