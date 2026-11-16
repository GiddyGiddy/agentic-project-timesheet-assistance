import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.utils import get_column_letter

from src.graph.state import ExcelLayout
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
