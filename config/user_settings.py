import ctypes
import json
import os
import sys
import tempfile
from ctypes import wintypes
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr, ValidationError, field_validator

SETTINGS_FILE = "service-settings.dpapi"


class UserConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, str_strip_whitespace=True)

    azure_openai_endpoint: str = Field(min_length=1, max_length=2048)
    azure_openai_deployment_name: str = Field(min_length=1, max_length=128)
    azure_openai_api_version: str = Field(default="2024-10-21", pattern=r"^\d{4}-\d{2}-\d{2}(?:-preview)?$")
    azure_openai_api_key: SecretStr | None = Field(default=None, max_length=8192)
    tenant_id: UUID
    client_id: UUID
    timezone: str = Field(default="Europe/Berlin", max_length=128)

    @field_validator("azure_openai_endpoint")
    @classmethod
    def resource_endpoint(cls, value: str) -> str:
        parsed = HttpUrl(value)
        if (
            parsed.scheme != "https" or not parsed.host or parsed.port == 0
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment
        ):
            raise ValueError("Use an HTTPS resource root without credentials, path, query or fragment")
        return str(parsed).rstrip("/")

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Use a valid IANA time zone") from exc
        return value

    @field_validator("azure_openai_api_key", mode="before")
    @classmethod
    def blank_key(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    def settings_values(self) -> dict[str, str | None]:
        values = self.model_dump(mode="json", exclude={"azure_openai_api_key"})
        values["azure_openai_api_key"] = (
            self.azure_openai_api_key.get_secret_value() if self.azure_openai_api_key else None
        )
        return values


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD),
        ("data", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _crypt(data: bytes, *, decrypt: bool) -> bytes:
    if sys.platform != "win32":
        raise OSError("Protected service settings require Windows DPAPI")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    operation = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    operation.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR) if decrypt else wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob), wintypes.LPVOID, wintypes.LPVOID,
        wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    operation.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    buffer = ctypes.create_string_buffer(data)
    source = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = _DataBlob()
    # UI_FORBIDDEN only: omitting LOCAL_MACHINE keeps protection user-scoped.
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        action = "read" if decrypt else "protect"
        raise OSError(
            ctypes.get_last_error(),
            f"Could not {action} Windows-protected service settings; use the original Windows account or reconfigure",
        )
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel32.LocalFree(output.data)


def read_protected_file(path: Path) -> bytes:
    if path.stat().st_size > 65536:
        raise ValueError("Protected file exceeds the expected size")
    return _crypt(path.read_bytes(), decrypt=True)


def write_protected_file(path: Path, plaintext: bytes) -> None:
    encrypted = _crypt(plaintext, decrypt=False)
    if len(encrypted) > 65536:
        raise ValueError("Protected file exceeds the expected size")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encrypted)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_user_configuration(root: Path) -> UserConfiguration | None:
    path = root / "config" / SETTINGS_FILE
    if not path.exists():
        return None
    try:
        return UserConfiguration.model_validate_json(read_protected_file(path))
    except ValidationError:
        raise ValueError("Protected service settings are invalid; reconfigure this profile") from None


def write_user_configuration(root: Path, configuration: UserConfiguration) -> None:
    write_protected_file(
        root / "config" / SETTINGS_FILE, json.dumps(configuration.settings_values()).encode("utf-8"),
    )
