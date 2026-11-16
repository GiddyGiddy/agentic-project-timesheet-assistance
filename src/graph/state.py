from typing import Any, Literal, TypedDict

import re

from openpyxl.utils import column_index_from_string
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Project(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1)
    client: str = Field(min_length=1)
    project_name: str | None = None
    domains: list[str] = []
    channels: list[str] = []
    keywords: list[str] = []


class ExcelLayout(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sheet_name: str
    header_row: int | None = Field(default=None, ge=1, le=1048576)
    project_column: int = Field(ge=1, le=16384)
    first_project_row: int = Field(ge=1, le=1048576)
    last_project_row: int | None = Field(default=None, ge=1, le=1048576)
    day_columns: dict[int, str] | None = None
    month_header_cell: str | None = None
    month_header_format: str = "%B %Y"
    monthly_total_column: str | None = None
    write_total_formula: bool = False

    @field_validator("day_columns")
    @classmethod
    def valid_day_columns(cls, value: dict[int, str] | None) -> dict[int, str] | None:
        if value is None:
            return None
        if not value or any(not 1 <= day <= 31 for day in value):
            raise ValueError("Day columns must map days 1 through 31")
        return {day: cls._column(letter) for day, letter in value.items()}

    @field_validator("monthly_total_column")
    @classmethod
    def valid_total_column(cls, value: str | None) -> str | None:
        return cls._column(value) if value is not None else None

    @staticmethod
    def _column(value: str) -> str:
        value = value.upper()
        if not re.fullmatch(r"[A-Z]{1,3}", value) or column_index_from_string(value) > 16384:
            raise ValueError(f"Invalid Excel column: {value}")
        return value

    @field_validator("month_header_cell")
    @classmethod
    def valid_month_header(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.upper()
        match = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]*)", value)
        if not match or int(match.group(2)) > 1048576:
            raise ValueError("Invalid month header cell")
        cls._column(match.group(1))
        return value

    @model_validator(mode="after")
    def valid_layout(self) -> "ExcelLayout":
        if self.header_row is None and self.day_columns is None:
            raise ValueError("Provide header_row or day_columns")
        if self.last_project_row is not None and self.last_project_row < self.first_project_row:
            raise ValueError("last_project_row must not precede first_project_row")
        if self.write_total_formula and self.monthly_total_column is None:
            raise ValueError("monthly_total_column is required to write total formulas")
        if self.day_columns:
            columns = [column_index_from_string(value) for value in self.day_columns.values()]
            if len(columns) != len(set(columns)) or self.project_column in columns:
                raise ValueError("Day columns must be distinct and not the project column")
            if self.monthly_total_column and column_index_from_string(self.monthly_total_column) in columns:
                raise ValueError("Monthly total column must not be a day column")
        if self.monthly_total_column and column_index_from_string(self.monthly_total_column) == self.project_column:
            raise ValueError("Monthly total column must not be the project column")
        return self


class Registry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    projects: list[Project] = Field(min_length=1)
    excel: ExcelLayout

    @field_validator("projects")
    @classmethod
    def unique_codes(cls, projects: list[Project]) -> list[Project]:
        codes = [project.code for project in projects]
        if len(set(codes)) != len(codes):
            raise ValueError("Project codes must be unique")
        return projects


class Entry(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    project_code: str = Field(min_length=1)
    hours: float = Field(gt=0, le=24)
    summary: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)

    @field_validator("hours")
    @classmethod
    def quarter_hours(cls, hours: float) -> float:
        if not (hours * 4).is_integer():
            raise ValueError("Hours must be in increments of 0.25")
        return hours


class MappedEntries(BaseModel):
    entries: list[Entry]


class TimesheetState(TypedDict, total=False):
    target_date: str
    raw_emails: list[dict[str, Any]]
    raw_events: list[dict[str, Any]]
    raw_chats: list[dict[str, Any]]
    mapped_entries: list[dict[str, Any]]
    approval_status: Literal["approved", "rejected"] | None
