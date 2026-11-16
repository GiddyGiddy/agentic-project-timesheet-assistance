import calendar
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.cell.cell import MergedCell
from openpyxl.utils import column_index_from_string, get_column_letter

from src.graph.state import ExcelLayout


def write_timesheet(
    template: Path,
    output: Path,
    year: int,
    month: int,
    totals: dict[tuple[str, int], float],
    project_codes: set[str],
    layout: ExcelLayout,
) -> None:
    last_day = calendar.monthrange(year, month)[1]
    if not template.is_file():
        raise FileNotFoundError(f"Excel template not found: {template}")
    if template.resolve() == output.resolve():
        raise ValueError("Output must not overwrite the original template")
    workbook = load_workbook(template)
    if layout.sheet_name not in workbook.sheetnames:
        raise ValueError(f"Worksheet not found: {layout.sheet_name}")
    sheet = workbook[layout.sheet_name]
    days: dict[int, int] = {}
    if layout.day_columns is not None:
        days = {day: column_index_from_string(col) for day, col in layout.day_columns.items()}
    elif layout.header_row is not None:
        for cell in sheet[layout.header_row]:
            value = cell.value
            if isinstance(value, (date, datetime)):
                day = value.day if (value.year, value.month) == (year, month) else None
            elif isinstance(value, int) and not isinstance(value, bool):
                day = value
            elif isinstance(value, str) and value.strip().isdigit():
                day = int(value.strip())
            else:
                day = None
            if day is not None and 1 <= day <= 31:
                if day in days:
                    raise ValueError(f"Duplicate day column: {day}")
                days[day] = cell.column
    if not set(range(1, last_day + 1)).issubset(days):
        raise ValueError("Template must contain a distinct column for every day of the month")
    rows: dict[str, int] = {}
    total_column = (
        column_index_from_string(layout.monthly_total_column)
        if layout.monthly_total_column else None
    )
    if layout.project_column in days.values() or total_column in days.values():
        raise ValueError("Project, day and monthly total columns must be distinct")
    last_row = min(layout.last_project_row or sheet.max_row, sheet.max_row)
    for row in range(layout.first_project_row, last_row + 1):
        value = sheet.cell(row, layout.project_column).value
        if isinstance(value, str) and value.strip() in project_codes:
            code = value.strip()
            if code in rows:
                raise ValueError(f"Duplicate project row: {code}")
            rows[code] = row
    missing = {code for code, _ in totals} - rows.keys()
    if missing:
        raise ValueError(f"Projects missing from template: {', '.join(sorted(missing))}")
    for (code, day), hours in totals.items():
        if code not in project_codes or day not in days or day > last_day or hours < 0:
            raise ValueError("Invalid project, day, or hours in monthly totals")
    for row in rows.values():
        for column in days.values():
            cell = sheet.cell(row, column)
            if cell.data_type == "f" or isinstance(cell, MergedCell):
                raise ValueError(f"Cannot write to formula or merged cell: {cell.coordinate}")
        if layout.write_total_formula and total_column is not None:
            cell = sheet.cell(row, total_column)
            if isinstance(cell, MergedCell) or (cell.value is not None and cell.data_type != "f"):
                raise ValueError(f"Cannot replace existing total cell: {cell.coordinate}")
    if layout.month_header_cell:
        cell = sheet[layout.month_header_cell]
        if (
            isinstance(cell, MergedCell)
            or cell.data_type == "f"
            or (layout.first_project_row <= cell.row <= last_row and (
                cell.column in days.values()
                or cell.column == layout.project_column
                or cell.column == total_column
            ))
            or (cell.row == layout.header_row and cell.column in days.values())
        ):
            raise ValueError(f"Cannot write month header to cell: {cell.coordinate}")
        cell.value = date(year, month, 1).strftime(layout.month_header_format)
    for code, row in rows.items():
        for day, column in days.items():
            sheet.cell(row, column).value = totals.get((code, day))
        if layout.write_total_formula and total_column is not None:
            cell = sheet.cell(row, total_column)
            if cell.value is None:
                references = ",".join(
                    f"{get_column_letter(column)}{row}" for _, column in sorted(days.items())
                )
                cell.value = f"=SUM({references})"
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as destination:
        try:
            workbook.save(destination)
        except Exception:
            destination.close()
            output.unlink()
            raise
