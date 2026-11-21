import pytest

from windows.verify_bundle import verify_bundle


@pytest.mark.parametrize("relative", [
    ".env", ".env.production", r"config\service-settings.dpapi",
    r"data\timesheet.db", r"data\checkpoints.sqlite-wal", r"data\checkpoints.sqlite3-shm",
    r"data\template.xlsx", r"run\desktop.log.1", r"run\desktop.lock", "signing.pfx",
    r".venv\pyvenv.cfg", r"tests\fixture.json",
])
def test_bundle_rejects_user_data_secrets_and_development_files(tmp_path, relative):
    forbidden = tmp_path / relative
    forbidden.parent.mkdir(parents=True, exist_ok=True)
    forbidden.write_bytes(b"synthetic build exclusion fixture")
    with pytest.raises(ValueError, match="forbidden development/user files"):
        verify_bundle(tmp_path)


def test_bundle_reports_missing_resources_without_rejecting_public_ca(tmp_path):
    certificate = tmp_path / "_internal" / "certifi" / "cacert.pem"
    certificate.parent.mkdir(parents=True)
    certificate.write_text("synthetic public CA fixture")
    with pytest.raises(ValueError, match="Bundle is incomplete:.*TimesheetAssistant.exe"):
        verify_bundle(tmp_path)
