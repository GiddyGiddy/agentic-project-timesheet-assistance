import calendar
import re
from datetime import date, datetime
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import MergedCell
from openpyxl.utils import column_index_from_string, get_column_letter
from openpyxl.utils.cell import coordinate_from_string

from src.graph.state import ExcelLayout, Project


def template_for_layout(template: Path, layout: ExcelLayout) -> Path:
    if layout.layout_mode == "rows":
        return template.with_name(f"{template.stem}_rowwise{template.suffix}")
    return template


def _new_template(project_codes: set[str], layout: ExcelLayout) -> Workbook:
    last_row = layout.first_project_row + len(project_codes) - 1
    if last_row > (layout.last_project_row or 1048576):
        raise ValueError("Not enough project rows in the configured Excel layout")

    header_row = layout.header_row
    if header_row is None and layout.first_project_row > 1:
        header_row = layout.first_project_row - 1
    if header_row is not None and layout.first_project_row <= header_row <= last_row:
        raise ValueError("The day header row overlaps project rows")

    workbook = Workbook()
    sheet = workbook.worksheets[0]
    sheet.title = layout.sheet_name
    month_location = None
    if layout.month_header_cell:
        letter, row = coordinate_from_string(layout.month_header_cell)
        month_location = (row, column_index_from_string(letter))
    total_column = (
        column_index_from_string(layout.monthly_total_column)
        if layout.monthly_total_column else None
    )
    if layout.day_columns is not None:
        days = {day: column_index_from_string(col) for day, col in layout.day_columns.items()}
    else:
        reserved = {layout.project_column, total_column}
        if month_location and month_location[0] == header_row:
            reserved.add(month_location[1])
        available = (column for column in range(1, 16385) if column not in reserved)
        days = {day: next(available) for day in range(1, 32)}

    if header_row is not None:
        header_columns = {layout.project_column, *days.values()}
        if total_column is not None:
            header_columns.add(total_column)
        if month_location and month_location[0] == header_row and month_location[1] in header_columns:
            raise ValueError("Month header overlaps generated column headers")
        if header_row < layout.first_project_row or "Project" not in project_codes:
            sheet.cell(header_row, layout.project_column, "Project")
        for day, column in days.items():
            sheet.cell(header_row, column, day)
        if total_column is not None:
            sheet.cell(header_row, total_column, "Total")

    for row, code in enumerate(sorted(project_codes), layout.first_project_row):
        cell = sheet.cell(row, layout.project_column, code)
        cell.data_type = "s"
    return workbook


def _save_new(workbook: Workbook, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as destination:
        try:
            workbook.save(destination)
        except Exception:
            destination.close()
            path.unlink()
            raise


def _row_template(layout: ExcelLayout) -> Workbook:
    workbook = Workbook()
    sheet = workbook.worksheets[0]
    sheet.title = layout.sheet_name
    sheet["A1"] = "Project"
    sheet["A2"] = "Project number"
    sheet["A3"] = "Monthly Timesheet"
    sheet["A4"] = "Weekday"
    sheet["B4"] = "Date"
    sheet["C4"] = "Hours"
    sheet.column_dimensions["A"].width = 20
    sheet.column_dimensions["B"].width = 32
    sheet.column_dimensions["C"].width = 15
    return workbook


def _project_sheet_title(code: str, existing: list[str]) -> str:
    base = re.sub(r"[\x00-\x1f\\/*?:\[\]]", "_", code).strip()[:31].strip(" '") or "Project"
    used = {name.casefold() for name in existing}
    title = base
    number = 2
    while title.casefold() in used:
        suffix = f" ({number})"
        title = base[:31 - len(suffix)] + suffix
        number += 1
    return title


def write_row_timesheet(
    template: Path,
    output: Path,
    year: int,
    month: int,
    totals: dict[tuple[str, int], float],
    projects: list[Project],
    layout: ExcelLayout,
) -> None:
    last_day = calendar.monthrange(year, month)[1]
    if template.resolve() == output.resolve():
        raise ValueError("Output must not overwrite the original template")
    codes = {project.code for project in projects}
    if not codes or any(
        code not in codes or not 1 <= day <= last_day or hours < 0
        for (code, day), hours in totals.items()
    ):
        raise ValueError("Invalid project, day, or hours in monthly totals")
    create_template = not template.exists()
    workbook = _row_template(layout) if create_template else load_workbook(template)
    if layout.sheet_name not in workbook.sheetnames:
        raise ValueError(f"Worksheet not found: {layout.sheet_name}")
    sheet = workbook[layout.sheet_name]
    for coordinate, label in (("A1", "Project"), ("A2", "Project number")):
        if sheet[coordinate].value != label:
            raise ValueError(f"Row-wise template requires '{label}' in {coordinate}")
    legacy = sheet["A4"].value == "Date" and sheet["B4"].value == "Hours"
    if legacy:
        for coordinate in ("C4", "B37", "C37"):
            cell = sheet[coordinate]
            if cell.value is not None or (coordinate == "C4" and isinstance(cell, MergedCell)):
                raise ValueError(f"Cannot adapt legacy row-wise template; clear or update {coordinate}")
        for row in range(5, 36):
            cell = sheet.cell(row, 3)
            if cell.value is not None or isinstance(cell, MergedCell):
                raise ValueError(f"Cannot adapt legacy row-wise template; clear or update {cell.coordinate}")
    else:
        for coordinate, label in (("A4", "Weekday"), ("B4", "Date"), ("C4", "Hours")):
            if sheet[coordinate].value != label:
                raise ValueError(f"Row-wise template requires '{label}' in {coordinate}")
    for coordinate in ("B1", "B2", "A3"):
        cell = sheet[coordinate]
        if isinstance(cell, MergedCell) or cell.data_type == "f":
            raise ValueError(f"Cannot write to formula or merged cell: {coordinate}")
    for row in range(5, 36):
        for column in (1, 2, 3):
            cell = sheet.cell(row, column)
            if isinstance(cell, MergedCell) or cell.data_type == "f":
                raise ValueError(f"Cannot write to formula or merged cell: {cell.coordinate}")
    if layout.write_total_formula:
        cell = sheet["C37"]
        if isinstance(cell, MergedCell) or (cell.value is not None and cell.data_type != "f"):
            raise ValueError(f"Cannot replace existing total cell: {cell.coordinate}")
        if isinstance(sheet["A37"], MergedCell):
            raise ValueError("Cannot write to merged cell: A37")
    if create_template:
        try:
            _save_new(workbook, template)
        except FileExistsError as exc:
            raise ValueError("Excel template appeared during export; retry without replacing it") from exc

    for project in projects:
        title = _project_sheet_title(project.code, workbook.sheetnames)
        project_sheet = workbook.copy_worksheet(sheet)
        project_sheet.title = title
        if legacy:
            project_sheet["A4"] = "Weekday"
            project_sheet["B4"] = "Date"
            project_sheet["C4"] = "Hours"
        for coordinate, value in (("B1", project.project_name), ("B2", project.code)):
            project_sheet[coordinate] = value
            if value is not None:
                project_sheet[coordinate].data_type = "s"
        project_sheet["A3"] = f"Monthly Timesheet - {date(year, month, 1):%B %Y}"
        for day in range(1, 32):
            day_value = date(year, month, day) if day <= last_day else None
            weekday_cell = project_sheet.cell(day + 4, 1)
            weekday_cell.value = day_value
            weekday_cell.number_format = "[$-409]dddd"
            date_cell = project_sheet.cell(day + 4, 2)
            date_cell.value = day_value
            date_cell.number_format = layout.date_format
            project_sheet.cell(day + 4, 3).value = totals.get((project.code, day))
        if layout.write_total_formula:
            if project_sheet["A37"].value is None:
                project_sheet["A37"] = "Total"
            if project_sheet["C37"].value is None:
                project_sheet["C37"] = "=SUM(C5:C35)"
    workbook.remove(sheet)
    _save_new(workbook, output)


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
    if template.resolve() == output.resolve():
        raise ValueError("Output must not overwrite the original template")
    create_template = not template.exists()
    workbook = _new_template(project_codes, layout) if create_template else load_workbook(template)
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
    if create_template:
        try:
            _save_new(workbook, template)
        except FileExistsError as exc:
            raise ValueError("Excel template appeared during export; retry without replacing it") from exc
    if layout.month_header_cell:
        cell = sheet[layout.month_header_cell]
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
    _save_new(workbook, output)
