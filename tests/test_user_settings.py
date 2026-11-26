import io
import sys
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from pydantic import ValidationError

from config.settings import Settings
from config.storage import initialize_user_storage
from config.user_settings import (
    SETTINGS_FILE, UserConfiguration, read_user_configuration, write_user_configuration,
)
from src.web.app import create_app
from src.web.scheduler import DashboardScheduler

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Requires native Windows user-scoped DPAPI")


@pytest.fixture
def payload():
    return {
        "azure_openai_endpoint": "https://synthetic.openai.azure.com",
        "azure_openai_deployment_name": "synthetic-deployment",
        "azure_openai_api_version": "2024-10-21",
        "azure_openai_api_key": "synthetic-key-not-a-real-credential",
        "tenant_id": "11111111-1111-1111-1111-111111111111",
        "client_id": "22222222-2222-2222-2222-222222222222",
        "timezone": "Europe/Berlin",
    }


@pytest.fixture
def profile(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    root = tmp_path / "profile"
    monkeypatch.setenv("TIMESHEET_DATA_DIR", str(root))
    initialize_user_storage(Settings())
    return root


@pytest.fixture
def client(profile):
    with TestClient(
        create_app("synthetic-token"), base_url="http://127.0.0.1:8765",
        client=("127.0.0.1", 12345), headers={"X-Timesheet-Token": "synthetic-token"},
    ) as dashboard:
        yield dashboard


@pytest.fixture
def workbook_bytes():
    stream = io.BytesIO()
    workbook = Workbook()
    workbook.active["A1"] = "Synthetic template"
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


@pytest.mark.parametrize("field,value", [
    ("azure_openai_endpoint", "http://example.invalid"),
    ("azure_openai_endpoint", "https://user:password@example.invalid"),
    ("azure_openai_endpoint", "https://example.invalid/path?secret=not-real"),
    ("azure_openai_endpoint", "https://invalid host"),
    ("tenant_id", "not-a-guid"),
    ("timezone", "Not/A_Timezone"),
])
def test_service_settings_validate_without_echoing_inputs(payload, field, value):
    with pytest.raises(ValidationError) as error:
        UserConfiguration.model_validate({**payload, field: value})
    assert payload["azure_openai_api_key"] not in str(error.value)
    assert "input_value=" not in str(error.value)


@windows_only
def test_native_dpapi_roundtrip_precedence_and_no_plaintext(profile, payload, monkeypatch):
    assert read_user_configuration(profile) is None
    write_user_configuration(profile, UserConfiguration.model_validate(payload))
    encrypted = (profile / "config" / SETTINGS_FILE).read_bytes()
    assert payload["azure_openai_api_key"].encode() not in encrypted
    assert payload["azure_openai_endpoint"].encode() not in encrypted
    configuration = read_user_configuration(profile)
    assert configuration is not None
    assert configuration.azure_openai_api_key is not None
    assert configuration.azure_openai_api_key.get_secret_value() == payload["azure_openai_api_key"]
    assert Settings().azure_openai_api_key.get_secret_value() == payload["azure_openai_api_key"]
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "synthetic-environment-key")
    assert Settings().azure_openai_api_key.get_secret_value() == "synthetic-environment-key"
    assert Settings(azure_openai_api_key="synthetic-constructor-key").azure_openai_api_key.get_secret_value() == "synthetic-constructor-key"
    assert Settings(azure_openai_api_key=None).azure_openai_api_key is None


@windows_only
def test_corrupt_profile_fails_instead_of_falling_back(profile, monkeypatch):
    (profile / "config" / SETTINGS_FILE).write_bytes(b"not-a-dpapi-blob")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "must-not-hide-corruption")
    with pytest.raises(OSError, match="Windows-protected"):
        Settings()


@windows_only
def test_failed_atomic_save_preserves_previous_configuration(profile, payload, monkeypatch):
    write_user_configuration(profile, UserConfiguration.model_validate(payload))
    original = (profile / "config" / SETTINGS_FILE).read_bytes()

    def fail_replace(source, destination):
        raise PermissionError("Synthetic replace failure")

    monkeypatch.setattr("config.user_settings.os.replace", fail_replace)
    with pytest.raises(PermissionError):
        write_user_configuration(profile, UserConfiguration.model_validate({
            **payload, "azure_openai_api_key": "synthetic-replacement-key",
        }))
    assert (profile / "config" / SETTINGS_FILE).read_bytes() == original
    assert not list((profile / "config").glob(".settings-*.tmp"))
    assert Settings().azure_openai_api_key.get_secret_value() == payload["azure_openai_api_key"]


@windows_only
def test_profile_setup_retains_clears_and_reloads_key_without_disclosure(client, profile, payload):
    status = client.get("/api/setup").json()["profile_setup"]
    assert not status["services_configured"]
    assert not status["template_available"]
    assert client.put("/api/setup", json=payload).status_code == 200
    response = client.get("/api/setup")
    assert response.json()["profile_setup"]["api_key_configured"]
    assert response.json()["profile_setup"]["settings_saved"]
    assert payload["azure_openai_api_key"] not in response.text
    assert "**********" not in response.text
    assert "azure_openai_api_key" not in response.json()["profile_setup"]
    blank_key = {**payload, "azure_openai_api_key": ""}
    assert client.put("/api/setup", json=blank_key).status_code == 200
    assert Settings().azure_openai_api_key.get_secret_value() == payload["azure_openai_api_key"]
    with TestClient(
        create_app("restart-token"), base_url="http://127.0.0.1:8765",
        client=("127.0.0.1", 12345), headers={"X-Timesheet-Token": "restart-token"},
    ) as restarted:
        assert restarted.get("/api/setup").json()["profile_setup"]["services_configured"]
        assert restarted.put("/api/setup", json={**blank_key, "clear_api_key": True}).status_code == 200
    assert Settings().azure_openai_api_key is None
    assert not client.get("/api/setup").json()["profile_setup"]["services_configured"]


@windows_only
def test_environment_overrides_are_visible_readonly_and_not_copied(client, payload, monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "synthetic-environment-only")
    monkeypatch.setenv("TENANT_ID", payload["tenant_id"])
    status = client.get("/api/setup").json()["profile_setup"]
    assert set(status["environment_overrides"]) == {"azure_openai_api_key", "tenant_id"}
    assert client.put("/api/setup", json=payload).status_code == 409
    blank_key = {**payload, "azure_openai_api_key": None}
    assert client.put("/api/setup", json={
        **blank_key, "tenant_id": "33333333-3333-3333-3333-333333333333",
    }).status_code == 409
    assert client.put("/api/setup", json=blank_key).status_code == 200
    monkeypatch.delenv("AZURE_OPENAI_API_KEY")
    assert Settings().azure_openai_api_key is None


@windows_only
def test_setup_save_failure_is_explicit_and_does_not_disclose_key(client, profile, payload, monkeypatch, caplog):
    assert client.put("/api/setup", json=payload).status_code == 200
    before = (profile / "config" / SETTINGS_FILE).read_bytes()

    def fail_save(root, configuration):
        raise OSError(payload["azure_openai_api_key"])

    monkeypatch.setattr("src.web.app.write_user_configuration", fail_save)
    response = client.put("/api/setup", json={**payload, "azure_openai_deployment_name": "not-saved"})
    assert response.status_code == 500
    assert payload["azure_openai_api_key"] not in response.text + caplog.text
    assert (profile / "config" / SETTINGS_FILE).read_bytes() == before


def test_setup_rejects_sensitive_invalid_input_and_limits_body(client, profile, payload):
    response = client.put("/api/setup", json={**payload, "tenant_id": "invalid"})
    assert response.status_code == 422
    assert payload["azure_openai_api_key"] not in response.text
    assert client.put("/api/setup", content=b"x" * 16384).status_code == 422
    assert client.put("/api/setup", content=b"x" * 16385).status_code == 413
    assert not (profile / "config" / SETTINGS_FILE).exists()


@pytest.mark.parametrize("method,path", [("PUT", "/api/setup"), ("POST", "/api/setup/template")])
def test_setup_mutations_require_dashboard_token_and_origin(client, method, path):
    assert client.request(method, path, headers={"X-Timesheet-Token": ""}).status_code == 403
    assert client.request(method, path, headers={"Origin": "https://untrusted.invalid"}).status_code == 403


def test_setup_changes_blocked_while_scheduler_running(client, payload, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(DashboardScheduler, "running", property(lambda self: True))
        assert client.put("/api/setup", json=payload).status_code == 409
        assert client.post("/api/setup/template", content=b"invalid").status_code == 409


def test_template_upload_validates_and_never_overwrites(client, profile, workbook_bytes):
    registry = client.get("/api/registry").json()
    registry["excel"]["layout_mode"] = "columns"
    assert client.put("/api/registry", json=registry).status_code == 200
    path = Settings().template_path
    assert client.post("/api/setup/template", content=b"not-a-workbook").status_code == 422
    assert not path.exists()
    assert client.post("/api/setup/template", content=workbook_bytes).status_code == 200
    assert path.read_bytes() == workbook_bytes
    assert client.get("/api/setup").json()["profile_setup"]["template_available"]
    assert client.post("/api/setup/template", content=b"replacement").status_code == 409
    assert path.read_bytes() == workbook_bytes


def test_upload_targets_selected_rowwise_template(client, profile, workbook_bytes):
    legacy = Settings().template_path
    registry = client.get("/api/registry").json()
    registry["excel"]["layout_mode"] = "columns"
    assert client.put("/api/registry", json=registry).status_code == 200
    assert client.post("/api/setup/template", content=workbook_bytes).status_code == 200
    registry["excel"]["layout_mode"] = "rows"
    assert client.put("/api/registry", json=registry).status_code == 200
    assert not client.get("/api/setup").json()["profile_setup"]["template_available"]
    assert client.post("/api/setup/template", content=workbook_bytes).status_code == 200
    rowwise = legacy.with_name(f"{legacy.stem}_rowwise{legacy.suffix}")
    assert rowwise.read_bytes() == workbook_bytes
    assert legacy.read_bytes() == workbook_bytes
    assert client.get("/api/setup").json()["profile_setup"]["template_available"]


@pytest.mark.parametrize("kind", ["macro", "macro-type", "expanded"])
def test_template_rejects_macros_and_zip_expansion(client, workbook_bytes, kind):
    stream = io.BytesIO()
    with ZipFile(io.BytesIO(workbook_bytes)) as source, ZipFile(stream, "w", ZIP_DEFLATED) as target:
        for item in source.infolist():
            content = source.read(item.filename)
            if kind == "macro-type" and item.filename == "[Content_Types].xml":
                content = content.replace(
                    b"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
                    b"application/vnd.ms-excel.sheet.macroEnabled.main+xml",
                )
            target.writestr(item.filename, content)
        if kind == "macro":
            target.writestr("xl/vbaProject.bin", b"synthetic-not-an-actual-macro")
        elif kind == "expanded":
            target.writestr("oversized.txt", b"x" * (50 * 1024 * 1024 + 1))
    assert client.post("/api/setup/template", content=stream.getvalue()).status_code == 422
    assert not Settings().template_path.exists()


def test_template_body_size_boundary(client):
    limit = 10 * 1024 * 1024
    assert client.post("/api/setup/template", content=b"x" * limit).status_code == 422
    assert client.post("/api/setup/template", content=b"x" * (limit + 1)).status_code == 413
