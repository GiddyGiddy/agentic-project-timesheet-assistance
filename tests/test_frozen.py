import http.client
import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from openpyxl import Workbook

from config.settings import Settings
from config.user_settings import SETTINGS_FILE, read_protected_file
from src.desktop import DesktopSession, SESSION_FILE

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or not os.environ.get("TIMESHEET_FROZEN_EXE"),
    reason="Opt-in Windows build test; requires TIMESHEET_FROZEN_EXE and free port 8765",
)


def test_frozen_desktop_runs_offline_without_python_and_preserves_profile(tmp_path):
    executable = Path(os.environ["TIMESHEET_FROZEN_EXE"]).resolve(strict=True)
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", 8765))
        except OSError:
            pytest.fail("Port 8765 is occupied; close the dashboard before the frozen test")

    profile = tmp_path / "profile"
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / ".env").write_text("TIMEZONE=Invalid/Zone\nDATABASE_URL=sqlite:///wrong.db\n")
    environment = os.environ.copy()
    for field in Settings.model_fields:
        environment.pop(field.upper(), None)
    for name in ("PYTHONHOME", "PYTHONPATH", "PYTHONUSERBASE", "VIRTUAL_ENV"):
        environment.pop(name, None)
    environment["PATH"] = os.pathsep.join([
        str(Path(environment["SYSTEMROOT"]) / "System32"), environment["SYSTEMROOT"],
    ])
    environment["TIMESHEET_DATA_DIR"] = str(profile)
    processes = []

    def start():
        process = subprocess.Popen(
            [str(executable)], cwd=foreign, env=environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        processes.append(process)
        return process

    def ready(process):
        deadline = time.monotonic() + 60
        path = profile / "run" / SESSION_FILE
        while time.monotonic() < deadline:
            assert process.poll() is None, "Frozen desktop exited before readiness"
            if path.exists():
                return DesktopSession.model_validate_json(read_protected_file(path))
            time.sleep(0.1)
        log = profile / "run" / "desktop.log"
        pytest.fail("Frozen startup timed out. " + (log.read_text() if log.exists() else "No desktop log."))

    def request(session, path, method="GET", body=None, content_type="application/json"):
        connection = http.client.HTTPConnection("127.0.0.1", session.port, timeout=30)
        try:
            connection.request(method, path, body=body, headers={
                "X-Timesheet-Token": session.token.get_secret_value(),
                "Content-Type": content_type,
            })
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    try:
        first = start()
        session = ready(first)
        for path in ("/", "/static/app.js", "/static/style.css"):
            status, content = request(session, path)
            assert status == 200 and content
        status, content = request(session, "/api/setup")
        assert status == 200
        setup = json.loads(content)["profile_setup"]
        assert setup["timezone"] == "Europe/Berlin"
        assert not setup["services_configured"] and not setup["api_key_configured"]
        assert Path(setup["data_directory"]) == profile
        assert (profile / "config" / "mcp_servers.json").is_file()
        assert (profile / "config" / "projects_registry.json").is_file()

        services = {
            "azure_openai_endpoint": "https://frozen-smoke.invalid",
            "azure_openai_deployment_name": "synthetic-deployment",
            "azure_openai_api_key": "synthetic-build-test-key-not-a-real-credential",
            "tenant_id": "11111111-1111-4111-8111-111111111111",
            "client_id": "22222222-2222-4222-8222-222222222222",
            "timezone": "Europe/London",
        }
        assert request(session, "/api/setup", "PUT", json.dumps(services))[0] == 200
        protected = profile / "config" / SETTINGS_FILE
        assert services["azure_openai_api_key"].encode() not in protected.read_bytes()
        assert services["azure_openai_api_key"].encode() in read_protected_file(protected)
        status, content = request(session, "/api/setup")
        assert status == 200 and services["azure_openai_api_key"].encode() not in content
        assert json.loads(content)["profile_setup"]["api_key_configured"]

        workbook = Workbook()
        template = io.BytesIO()
        workbook.save(template)
        assert request(session, "/api/setup/template", "POST", template.getvalue(),
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")[0] == 200
        assert request(session, "/api/setup/template", "POST", template.getvalue())[0] == 409

        # A missing read-only workflow still builds the real graph and SQLite checkpoint schema.
        status, content = request(session, "/api/daily/2000-01-02")
        assert status == 400 and json.loads(content)["detail"] == "No workflow found for this date"
        for name in ("timesheet.db", "checkpoints.db"):
            database = profile / "data" / name
            assert database.is_file()
            with sqlite3.connect(database) as connection:
                assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        status, content = request(session, "/api/scheduler")
        assert status == 200 and not json.loads(content)["running"]
        assert not (foreign / "wrong.db").exists()

        duplicate = start()
        assert duplicate.wait(timeout=30) == 0
        assert first.poll() is None
        assert request(session, "/api/lifecycle", "POST")[0] == 202
        assert first.wait(timeout=30) == 0
        assert not (profile / "run" / SESSION_FILE).exists()

        restarted = start()
        current = ready(restarted)
        assert current.token != session.token
        status, content = request(current, "/api/setup")
        assert status == 200
        saved = json.loads(content)["profile_setup"]
        assert saved["timezone"] == "Europe/London" and saved["settings_saved"]
        assert saved["api_key_configured"] and saved["template_available"]
        assert request(current, "/api/lifecycle", "POST")[0] == 202
        assert restarted.wait(timeout=30) == 0
        assert not (profile / "run" / SESSION_FILE).exists()
    finally:
        for process in processes:
            if process.poll() is None:
                subprocess.run([
                    "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                    f"Stop-Process -Id {process.pid} -Force -ErrorAction Stop",
                ], check=True, capture_output=True)
                process.wait(timeout=15)
