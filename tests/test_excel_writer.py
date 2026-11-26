from datetime import datetime
from zipfile import BadZipFile

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.utils import get_column_letter

from src.graph.state import ExcelLayout, Project
from src.integrations import excel_writer
from src.integrations.excel_writer import write_timesheet


@pytest.fixture
def excel(tmp_path):
    template = tmp_path / "template.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Timesheet"
    sheet.cell(1, 1, "Project")
    for day in range(1, 32):
        sheet.cell(1, day + 1, day)
    sheet.cell(2, 1, "P1")
    sheet.cell(2, 3, 99)
    sheet.cell(2, 32, 99)
    sheet.cell(2, 33, "=SUM(B2:AF2)")
    workbook.save(template)
    layout = ExcelLayout(
        sheet_name="Timesheet", header_row=1, project_column=1, first_project_row=2
    )
    return template, tmp_path / "out.xlsx", layout


def test_export_preserves_original_and_formula(excel):
    template, output, layout = excel
    original = template.read_bytes()
    write_timesheet(template, output, 2026, 11, {("P1", 1): 2.25}, {"P1"}, layout)
    sheet = load_workbook(output).active
    assert sheet["B2"].value == 2.25
    assert sheet["C2"].value is None
    assert sheet["AF2"].value is None
    assert sheet["AG2"].value == "=SUM(B2:AF2)"
    assert template.read_bytes() == original
    with pytest.raises(FileExistsError):
        write_timesheet(template, output, 2026, 11, {}, {"P1"}, layout)


def test_export_fails_for_missing_project_or_formula_input(excel):
    template, output, layout = excel
    with pytest.raises(ValueError, match="missing from template"):
        write_timesheet(template, output, 2026, 11, {("P2", 1): 1}, {"P1", "P2"}, layout)
    workbook = load_workbook(template)
    workbook.active["B2"] = "=1+1"
    workbook.save(template)
    with pytest.raises(ValueError, match="formula or merged"):
        write_timesheet(template, output, 2026, 11, {("P1", 1): 1}, {"P1"}, layout)
    assert not output.exists()


def test_explicit_layout_month_header_and_total_formula(tmp_path):
    template = tmp_path / "template.xlsx"
    output = tmp_path / "output.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Timesheet"
    sheet["A6"] = "P1"
    sheet["A7"] = "P2"
    sheet["A8"] = "P1"  # Outside the configured project range.
    sheet["C6"] = 99
    sheet["AH7"] = "=SUM(C7:AG7)"
    workbook.save(template)
    layout = ExcelLayout(
        sheet_name="Timesheet",
        project_column=1,
        first_project_row=6,
        last_project_row=7,
        day_columns={day: get_column_letter(day + 2) for day in range(1, 32)},
        month_header_cell="B2",
        monthly_total_column="AH",
        write_total_formula=True,
    )
    write_timesheet(template, output, 2026, 11, {("P1", 2): 1.5}, {"P1", "P2"}, layout)
    sheet = load_workbook(output).active
    assert sheet["B2"].value == "November 2026"
    assert sheet["C6"].value is None
    assert sheet["D6"].value == 1.5
    assert sheet["AH6"].value.startswith("=SUM(C6,D6,")
    assert sheet["AH7"].value == "=SUM(C7:AG7)"
    assert load_workbook(template).active["C6"].value == 99


def test_layout_rejects_collisions_and_invalid_cells(excel):
    template, output, _ = excel
    with pytest.raises(ValueError, match="distinct"):
        ExcelLayout(
            sheet_name="Timesheet", project_column=1, first_project_row=2,
            day_columns={1: "B", 2: "B"},
        )
    with pytest.raises(ValueError, match="Invalid month header"):
        ExcelLayout(
            sheet_name="Timesheet", project_column=1, first_project_row=2,
            header_row=1, month_header_cell="A1048577",
        )
    layout = ExcelLayout(
        sheet_name="Timesheet", project_column=1, first_project_row=2,
        header_row=1, month_header_cell="B2",
    )
    with pytest.raises(ValueError, match="month header"):
        write_timesheet(template, output, 2026, 11, {("P1", 1): 1}, {"P1"}, layout)
    assert not output.exists()


def test_formula_generation_refuses_to_replace_literal_value(excel):
    template, output, _ = excel
    workbook = load_workbook(template)
    workbook.active["AG2"] = 100
    workbook.save(template)
    layout = ExcelLayout(
        sheet_name="Timesheet", project_column=1, first_project_row=2,
        header_row=1, monthly_total_column="AG", write_total_formula=True,
    )
    with pytest.raises(ValueError, match="existing total"):
        write_timesheet(template, output, 2026, 11, {("P1", 1): 1}, {"P1"}, layout)


def test_missing_template_is_created_with_reusable_day_headers_and_projects(tmp_path):
    template = tmp_path / "data" / "templates" / "template.xlsx"
    output = tmp_path / "output.xlsx"
    layout = ExcelLayout(
        sheet_name="Work", header_row=3, project_column=2, first_project_row=4,
        month_header_cell="A1", monthly_total_column="C", write_total_formula=True,
    )
    write_timesheet(template, output, 2026, 11, {("P2", 30): 2.5}, {"P2", "P1"}, layout)

    original = load_workbook(template)["Work"]
    result = load_workbook(output)["Work"]
    assert [original.cell(3, col).value for col in (1, 4, 33)] == [1, 2, 31]
    assert [original["B4"].value, original["B5"].value] == ["P1", "P2"]
    assert original["A1"].value is None
    assert result["A1"].value == "November 2026"
    assert result.cell(5, 32).value == 2.5
    assert result["C5"].value.startswith("=SUM(")
    original_bytes = template.read_bytes()
    write_timesheet(template, tmp_path / "february.xlsx", 2028, 2, {("P1", 29): 3}, {"P1", "P2"}, layout)
    assert template.read_bytes() == original_bytes
    assert load_workbook(tmp_path / "february.xlsx")["Work"].cell(4, 31).value == 3


def test_generated_template_respects_explicit_columns_and_row_limit(tmp_path):
    template = tmp_path / "template.xlsx"
    output = tmp_path / "output.xlsx"
    layout = ExcelLayout(
        sheet_name="Work", header_row=5, project_column=1,
        first_project_row=6, last_project_row=7,
        day_columns={day: get_column_letter(day + 2) for day in range(1, 32)},
        monthly_total_column="AH", write_total_formula=True,
    )
    write_timesheet(template, output, 2026, 11, {("P2", 2): 1.5}, {"P1", "P2"}, layout)
    sheet = load_workbook(output)["Work"]
    assert sheet["C5"].value == 1
    assert sheet["AG5"].value == 31
    assert sheet["A7"].value == "P2"
    assert sheet["D7"].value == 1.5
    assert sheet["AH7"].value.startswith("=SUM(")


def test_generated_template_rejects_insufficient_rows_without_creating_files(tmp_path):
    template = tmp_path / "template.xlsx"
    output = tmp_path / "output.xlsx"
    layout = ExcelLayout(
        sheet_name="Work", header_row=1, project_column=1,
        first_project_row=2, last_project_row=2,
    )
    with pytest.raises(ValueError, match="project rows"):
        write_timesheet(template, output, 2026, 11, {("P1", 1): 1}, {"P1", "P2"}, layout)
    assert not template.exists()
    assert not output.exists()


def test_generated_template_rejects_header_overlapping_project_rows(tmp_path):
    template = tmp_path / "template.xlsx"
    layout = ExcelLayout(
        sheet_name="Work", header_row=2, project_column=1, first_project_row=2,
    )
    with pytest.raises(ValueError, match="header row"):
        write_timesheet(template, tmp_path / "output.xlsx", 2026, 11, {("P1", 1): 1}, {"P1"}, layout)
    assert not template.exists()


def test_generated_template_adds_headers_for_explicit_mapping_without_header_row(tmp_path):
    template = tmp_path / "template.xlsx"
    layout = ExcelLayout(
        sheet_name="Work", project_column=1, first_project_row=3,
        day_columns={day: get_column_letter(day + 1) for day in range(1, 32)},
    )
    write_timesheet(template, tmp_path / "out.xlsx", 2026, 11, {("=P1", 1): 2}, {"=P1"}, layout)
    sheet = load_workbook(template)["Work"]
    assert sheet["B2"].value == 1
    assert sheet["A3"].value == "=P1"
    assert sheet["A3"].data_type == "s"


def test_generated_template_rejects_incomplete_mapping_and_overlapping_month_header(tmp_path):
    template = tmp_path / "template.xlsx"
    mapping = {day: get_column_letter(day + 1) for day in range(1, 30)}
    layout = ExcelLayout(
        sheet_name="Work", project_column=1, first_project_row=3,
        day_columns=mapping,
    )
    with pytest.raises(ValueError, match="every day"):
        write_timesheet(template, tmp_path / "out.xlsx", 2026, 11, {("P1", 1): 1}, {"P1"}, layout)
    assert not template.exists()
    layout = layout.model_copy(update={"month_header_cell": "B2"})
    with pytest.raises(ValueError, match="Month header overlaps"):
        write_timesheet(template, tmp_path / "out.xlsx", 2026, 2, {("P1", 1): 1}, {"P1"}, layout)
    assert not template.exists()


def test_existing_invalid_template_is_not_replaced(tmp_path):
    template = tmp_path / "template.xlsx"
    template.write_bytes(b"not an Excel workbook")
    layout = ExcelLayout(sheet_name="Work", header_row=1, project_column=1, first_project_row=2)
    with pytest.raises(BadZipFile):
        write_timesheet(template, tmp_path / "out.xlsx", 2026, 11, {("P1", 1): 1}, {"P1"}, layout)
    assert template.read_bytes() == b"not an Excel workbook"


def test_template_created_concurrently_is_not_replaced(tmp_path, monkeypatch):
    template = tmp_path / "template.xlsx"
    original = excel_writer._new_template

    def create_concurrently(project_codes, layout):
        workbook = original(project_codes, layout)
        template.write_bytes(b"another template")
        return workbook

    monkeypatch.setattr(excel_writer, "_new_template", create_concurrently)
    layout = ExcelLayout(sheet_name="Work", header_row=1, project_column=1, first_project_row=2)
    with pytest.raises(ValueError, match="template appeared during export"):
        write_timesheet(template, tmp_path / "out.xlsx", 2026, 11, {("P1", 1): 1}, {"P1"}, layout)
    assert template.read_bytes() == b"another template"


@pytest.mark.parametrize("date_format", [
    "dd/mm/yyyy", "dddd, d mmmm yyyy", "dd.mm.yy", "dddd, dd.mm.yy",
])
def test_rowwise_export_uses_real_dates_and_one_sheet_per_project(tmp_path, date_format):
    template = tmp_path / "rowwise.xlsx"
    output = tmp_path / "output.xlsx"
    layout = ExcelLayout(
        layout_mode="rows", sheet_name="Timesheet", project_column=1,
        first_project_row=2, header_row=1, date_format=date_format,
        write_total_formula=True,
    )
    projects = [
        Project(code="P1", client="Client", project_name="Migration"),
        Project(code="P2", client="Client", project_name="Support"),
    ]
    excel_writer.write_row_timesheet(
        template, output, 2026, 11, {("P1", 2): 2.5, ("P2", 30): 1.25}, projects, layout,
    )
    workbook = load_workbook(output)
    assert len(workbook.worksheets) == 2
    first, second = workbook.worksheets
    assert [first["A1"].value, first["B1"].value, first["A2"].value, first["B2"].value] == [
        "Project", "Migration", "Project number", "P1",
    ]
    assert "November 2026" in first["A3"].value
    assert [first["A4"].value, first["B4"].value, first["C4"].value] == [
        "Weekday", "Date", "Hours",
    ]
    assert first["A6"].value == datetime(2026, 11, 2)
    assert first["A6"].number_format == "[$-409]dddd"
    assert first["B6"].value == datetime(2026, 11, 2)
    assert first["B6"].number_format == date_format
    assert first["C6"].value == 2.5
    assert second["A34"].value == datetime(2026, 11, 30)
    assert second["B34"].value == datetime(2026, 11, 30)
    assert second["C34"].value == 1.25
    assert first["A35"].value is None
    assert first["B35"].value is None
    assert first["C35"].value is None
    assert first["C37"].value == "=SUM(C5:C35)"
    assert load_workbook(template)["Timesheet"]["B1"].value is None


def test_legacy_date_locale_is_ignored_when_exporting(tmp_path):
    layout = ExcelLayout(
        layout_mode="rows", sheet_name="Timesheet", project_column=1,
        first_project_row=2, header_row=1, date_format="dd.mm.yyyy",
        date_locale="de-DE",
    )
    assert "date_locale" not in layout.model_dump()
    output = tmp_path / "output.xlsx"
    excel_writer.write_row_timesheet(
        tmp_path / "template.xlsx", output, 2026, 11, {("P1", 1): 2},
        [Project(code="P1", client="Client")], layout,
    )
    sheet = load_workbook(output).worksheets[0]
    assert sheet["A5"].value == datetime(2026, 11, 1)
    assert sheet["A5"].number_format == "[$-409]dddd"
    assert sheet["B5"].value == datetime(2026, 11, 1)
    assert sheet["B5"].number_format == "dd.mm.yyyy"
    assert sheet["C5"].value == 2


def test_rowwise_export_reuses_template_and_handles_leap_year_and_sheet_names(tmp_path):
    template = tmp_path / "rowwise.xlsx"
    projects = [
        Project(code="A/B", client="Client"),
        Project(code="a\\b", client="Client"),
    ]
    layout = ExcelLayout(
        layout_mode="rows", sheet_name="Timesheet", project_column=1,
        first_project_row=2, header_row=1,
    )
    excel_writer.write_row_timesheet(template, tmp_path / "nov.xlsx", 2026, 11, {}, projects, layout)
    before = template.read_bytes()
    excel_writer.write_row_timesheet(
        template, tmp_path / "feb.xlsx", 2028, 2, {("A/B", 29): 3}, projects, layout,
    )
    workbook = load_workbook(tmp_path / "feb.xlsx")
    assert len({name.casefold() for name in workbook.sheetnames}) == 2
    assert all(not any(char in name for char in "\\/*?:[]") and len(name) <= 31 for name in workbook.sheetnames)
    assert workbook.worksheets[0]["A33"].value == datetime(2028, 2, 29)
    assert workbook.worksheets[0]["B33"].value == datetime(2028, 2, 29)
    assert workbook.worksheets[0]["C33"].value == 3
    assert workbook.worksheets[0]["A34"].value is None
    assert template.read_bytes() == before


def test_rowwise_rejects_non_date_format_and_unsafe_template_cells(tmp_path):
    with pytest.raises(ValueError, match="date format"):
        ExcelLayout(
            layout_mode="rows", sheet_name="Timesheet", project_column=1,
            first_project_row=2, header_row=1, date_format="0.00",
        )
    template = tmp_path / "template.xlsx"
    workbook = Workbook()
    workbook.active.title = "Timesheet"
    workbook.active["A1"] = "Project"
    workbook.active["A2"] = "Project number"
    workbook.active["A4"] = "Weekday"
    workbook.active["B4"] = "Date"
    workbook.active["C4"] = "Hours"
    workbook.active["C6"] = "=1+1"
    workbook.save(template)
    layout = ExcelLayout(
        layout_mode="rows", sheet_name="Timesheet", project_column=1,
        first_project_row=2, header_row=1,
    )
    with pytest.raises(ValueError, match="formula or merged"):
        excel_writer.write_row_timesheet(
            template, tmp_path / "out.xlsx", 2026, 11, {("P1", 2): 1},
            [Project(code="P1", client="Client")], layout,
        )
    assert not (tmp_path / "out.xlsx").exists()


def test_rowwise_rejects_invalid_template_sheet_name():
    with pytest.raises(ValueError, match="worksheet name"):
        ExcelLayout(
            layout_mode="rows", sheet_name="x" * 32, project_column=1,
            first_project_row=2, header_row=1,
        )


def test_rowwise_preserves_existing_total_formula(tmp_path):
    template = tmp_path / "template.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Timesheet"
    for cell, label in (("A1", "Project"), ("A2", "Project number"), ("A4", "Weekday"),
                        ("B4", "Date"), ("C4", "Hours")):
        sheet[cell] = label
    sheet["C37"] = "=SUM(C5:C35)*2"
    workbook.save(template)
    original = template.read_bytes()
    layout = ExcelLayout(
        layout_mode="rows", sheet_name="Timesheet", project_column=1,
        first_project_row=2, header_row=1, write_total_formula=True,
    )
    excel_writer.write_row_timesheet(
        template, tmp_path / "out.xlsx", 2026, 11, {("P1", 1): 2},
        [Project(code="P1", client="Client")], layout,
    )
    assert load_workbook(tmp_path / "out.xlsx").worksheets[0]["C37"].value == "=SUM(C5:C35)*2"
    assert template.read_bytes() == original


def test_rowwise_reuses_simple_legacy_template_without_overwriting_it(tmp_path):
    template = tmp_path / "template.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Timesheet"
    for cell, label in (("A1", "Project"), ("A2", "Project number"), ("A4", "Date"), ("B4", "Hours")):
        sheet[cell] = label
    workbook.save(template)
    original = template.read_bytes()
    layout = ExcelLayout(
        layout_mode="rows", sheet_name="Timesheet", project_column=1,
        first_project_row=2, header_row=1, write_total_formula=True,
    )
    excel_writer.write_row_timesheet(
        template, tmp_path / "out.xlsx", 2026, 11, {("P1", 1): 2},
        [Project(code="P1", client="Client")], layout,
    )
    output = load_workbook(tmp_path / "out.xlsx").worksheets[0]
    assert [output["A4"].value, output["B4"].value, output["C4"].value] == ["Weekday", "Date", "Hours"]
    assert output["A5"].number_format == "[$-409]dddd"
    assert output["B5"].value == datetime(2026, 11, 1)
    assert output["C5"].value == 2
    assert output["C37"].value == "=SUM(C5:C35)"
    assert template.read_bytes() == original


@pytest.mark.parametrize("occupied", ["C4", "C5", "B37", "C37"])
def test_rowwise_rejects_legacy_template_when_migration_would_overwrite_data(tmp_path, occupied):
    template = tmp_path / "template.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Timesheet"
    for cell, label in (("A1", "Project"), ("A2", "Project number"), ("A4", "Date"), ("B4", "Hours")):
        sheet[cell] = label
    sheet[occupied] = "=1+1" if occupied == "B37" else "keep"
    workbook.save(template)
    original = template.read_bytes()
    layout = ExcelLayout(
        layout_mode="rows", sheet_name="Timesheet", project_column=1,
        first_project_row=2, header_row=1,
    )
    with pytest.raises(ValueError, match="legacy row-wise template"):
        excel_writer.write_row_timesheet(
            template, tmp_path / "out.xlsx", 2026, 11, {("P1", 1): 2},
            [Project(code="P1", client="Client")], layout,
        )
    assert template.read_bytes() == original
    assert not (tmp_path / "out.xlsx").exists()
