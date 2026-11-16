from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    azure_openai_api_key: SecretStr | None = None
    azure_openai_endpoint: str | None = None
    azure_openai_deployment_name: str | None = None
    azure_openai_api_version: str = "2024-10-21"
    tenant_id: str | None = None
    client_id: str | None = None
    client_secret: SecretStr | None = None
    timezone: str = "Europe/Berlin"
    database_url: str = "sqlite:///data/timesheet.db"
    checkpoint_path: Path = Path("data/checkpoints.db")
    mcp_config_path: Path = Path("config/mcp_servers.json")
    projects_registry_path: Path = Path("config/projects_registry.json")
    template_path: Path = Path("data/templates/timesheet_template.xlsx")
    output_dir: Path = Path("output")

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value
