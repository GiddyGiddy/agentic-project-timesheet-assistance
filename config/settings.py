import os
import sys
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from config.user_settings import read_user_configuration


def default_user_data_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise ValueError("LOCALAPPDATA is required for installed application storage")
    return Path(local_app_data) / "TimesheetAssistant"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    azure_openai_api_key: SecretStr | None = None
    azure_openai_endpoint: str | None = None
    azure_openai_deployment_name: str | None = None
    azure_openai_api_version: str = "2024-10-21"
    tenant_id: str | None = None
    client_id: str | None = None
    client_secret: SecretStr | None = None
    timesheet_data_dir: Path | None = None
    timezone: str = "Europe/Berlin"
    database_url: str = "sqlite:///data/timesheet.db"
    checkpoint_path: Path = Path("data/checkpoints.db")
    mcp_config_path: Path = Path("config/mcp_servers.json")
    projects_registry_path: Path = Path("config/projects_registry.json")
    template_path: Path = Path("data/templates/timesheet_template.xlsx")
    output_dir: Path = Path("output")

    def __init__(self, **values: Any) -> None:
        data_dir = values.get("timesheet_data_dir", os.environ.get("TIMESHEET_DATA_DIR"))
        if data_dir is None and getattr(sys, "frozen", False):
            data_dir = default_user_data_dir()
        if data_dir is not None:
            values["timesheet_data_dir"] = data_dir
            values.setdefault("_env_file", None)
            root = Path(data_dir).expanduser()
            if not root.is_absolute():
                raise ValueError("TIMESHEET_DATA_DIR must be an absolute path")
            configuration = read_user_configuration(root)
            if configuration is not None:
                environment = {key.casefold() for key in os.environ}
                for key, value in configuration.settings_values().items():
                    if key not in values and key.casefold() not in environment:
                        values[key] = value
        super().__init__(**values)

    @model_validator(mode="after")
    def resolve_user_paths(self) -> "Settings":
        if self.timesheet_data_dir is None:
            return self
        root = self.timesheet_data_dir.expanduser()
        if not root.is_absolute():
            raise ValueError("TIMESHEET_DATA_DIR must be an absolute path")
        root = root.resolve()
        paths = {
            "database_url": f"sqlite:///{root / 'data' / 'timesheet.db'}",
            "checkpoint_path": root / "data" / "checkpoints.db",
            "mcp_config_path": root / "config" / "mcp_servers.json",
            "projects_registry_path": root / "config" / "projects_registry.json",
            "template_path": root / "data" / "templates" / "timesheet_template.xlsx",
            "output_dir": root / "output",
        }
        for field, value in paths.items():
            if field not in self.model_fields_set:
                setattr(self, field, value)
            elif field != "database_url":
                configured = getattr(self, field).expanduser()
                setattr(self, field, (root / configured).resolve())
        url = make_url(self.database_url)
        if url.get_backend_name() == "sqlite" and url.database not in (None, "", ":memory:"):
            database = Path(url.database).expanduser()
            if not database.is_absolute():
                self.database_url = url.set(database=str((root / database).resolve())).render_as_string()
        self.timesheet_data_dir = root
        return self

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value
