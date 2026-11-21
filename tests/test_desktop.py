import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from config.settings import Settings
from config.user_settings import read_protected_file, write_protected_file
from src.desktop import DesktopSession, SESSION_FILE, _open_browser, _profile_lock, _reopen_existing, run_desktop

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Requires native Windows locking and DPAPI")
REPOSITORY = Path(__file__).resolve().parents[1]
CHILD = """
import http.client
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import src.desktop as desktop

root = Path(os.environ["TIMESHEET_DATA_DIR"])

def open_browser(url):
    parsed = urlsplit(url)
    token = parse_qs(parsed.fragment)["token"][0]
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    try:
        connection.request("GET", "/api/lifecycle", headers={"X-Timesheet-Token": token})
        assert connection.getresponse().status == 200, "Browser was opened before readiness"
    finally:
        connection.close()
    with (root / "browser-opened.txt").open("a") as stream:
        stream.write("opened\\n")
    return True

desktop.webbrowser.open = open_browser
desktop.run_desktop(port=int(os.environ.get("TEST_DESKTOP_PORT", "0")))
"""


def request(session, method="GET", path="/api/lifecycle", *, body=None, token=None):
    connection = http.client.HTTPConnection("127.0.0.1", session.port, timeout=5)
    try:
        connection.request(method, path, body=body, headers={
            "X-Timesheet-Token": token or session.token.get_secret_value(),
            "Content-Type": "application/json",
        })
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


@pytest.fixture
def children(tmp_path):
    processes = []
    environment = os.environ.copy()
    for field in Settings.model_fields:
        environment.pop(field.upper(), None)
    environment["TIMESHEET_DATA_DIR"] = str(tmp_path)

    def start(port=0):
        process = subprocess.Popen(
            [sys.executable, "-c", CHILD], cwd=REPOSITORY,
            env={**environment, "TEST_DESKTOP_PORT": str(port)},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        processes.append(process)
        return process

    yield start
    for process in processes:
        if process.poll() is None:
            stop_process(process)
        process.communicate(timeout=10)


def stop_process(process):
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
         f"Stop-Process -Id {process.pid} -Force -ErrorAction Stop"],
        check=True, capture_output=True,
    )
    process.wait(timeout=10)


def ready(root, process, expected_opens):
    deadline = time.monotonic() + 30
    path = root / "run" / SESSION_FILE
    while time.monotonic() < deadline:
        if process.poll() is not None:
            _, stderr = process.communicate()
            pytest.fail(f"Desktop startup failed: {stderr.decode(errors='replace')}")
        marker = root / "browser-opened.txt"
        if path.exists() and marker.exists() and len(marker.read_text().splitlines()) >= expected_opens:
            return DesktopSession.model_validate_json(read_protected_file(path))
        time.sleep(0.05)
    pytest.fail("Desktop did not publish a ready session and open the browser")


def test_real_launcher_reopens_exits_preserves_profile_and_recovers_after_crash(tmp_path, children):
    first = children()
    session = ready(tmp_path, first, 1)
    encrypted = (tmp_path / "run" / SESSION_FILE).read_bytes()
    assert session.token.get_secret_value().encode() not in encrypted
    assert request(session)[0] == 200
    registry = request(session, path="/api/registry")[1]
    registry["projects"][0]["client"] = "Synthetic persisted client"
    assert request(session, "PUT", "/api/registry", body=json.dumps(registry))[0] == 200

    duplicate = children()
    assert duplicate.wait(timeout=30) == 0
    assert len((tmp_path / "browser-opened.txt").read_text().splitlines()) == 2
    assert first.poll() is None
    assert request(session)[0] == 200

    assert request(session, "POST")[0] == 202
    assert first.wait(timeout=15) == 0
    assert not (tmp_path / "run" / SESSION_FILE).exists()

    second = children()
    restarted = ready(tmp_path, second, 3)
    assert restarted.token != session.token
    assert request(restarted, token=session.token.get_secret_value())[0] == 403
    assert request(restarted, path="/api/registry")[1]["projects"][0]["client"] == "Synthetic persisted client"
    stop_process(second)
    assert (tmp_path / "run" / SESSION_FILE).exists()

    third = children()
    recovered = ready(tmp_path, third, 4)
    assert recovered.token != restarted.token
    assert request(recovered, "POST")[0] == 202
    assert third.wait(timeout=15) == 0
    assert not (tmp_path / "run" / SESSION_FILE).exists()
    log = (tmp_path / "run" / "desktop.log").read_text()
    assert all(item.token.get_secret_value() not in log for item in (session, restarted, recovered))


def test_unrelated_port_is_not_reused_or_stopped(tmp_path, children):
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        child = children(occupied.getsockname()[1])
        _, stderr = child.communicate(timeout=30)
        assert child.returncode != 0
        assert b"port is unavailable" in stderr
        assert not (tmp_path / "browser-opened.txt").exists()
        assert not (tmp_path / "run" / SESSION_FILE).exists()
        assert occupied.getsockname()[1] != 0


def test_existing_session_requires_authenticated_nonredirecting_probe(tmp_path, monkeypatch):
    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "https://example.invalid/must-not-follow")
            self.end_headers()

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    opened = []
    monkeypatch.setattr("src.desktop.webbrowser.open", lambda url: opened.append(url) or True)
    try:
        with _profile_lock(tmp_path) as acquired:
            assert acquired
            write_protected_file(tmp_path / "run" / SESSION_FILE, json.dumps({
                "port": server.server_port, "token": "synthetic-probe-token-not-a-real-credential",
            }).encode())
            with pytest.raises(RuntimeError, match="could not be verified"):
                _reopen_existing(tmp_path, timeout=2)
        assert not opened
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_browser_failure_releases_socket_lock_and_session(tmp_path, monkeypatch):
    monkeypatch.setenv("TIMESHEET_DATA_DIR", str(tmp_path))
    for field in Settings.model_fields:
        if field != "timesheet_data_dir":
            monkeypatch.delenv(field.upper(), raising=False)
    monkeypatch.setattr("src.desktop.webbrowser.open", lambda url: False)
    with pytest.raises(RuntimeError, match="default browser"):
        run_desktop(data_dir=tmp_path, port=0)
    assert not (tmp_path / "run" / SESSION_FILE).exists()
    with _profile_lock(tmp_path) as acquired:
        assert acquired


def test_browser_errors_do_not_disclose_the_private_url(monkeypatch):
    session = DesktopSession(port=8765, token="synthetic-browser-error-token-not-real")

    def fail(url):
        raise OSError(url)

    monkeypatch.setattr("src.desktop.webbrowser.open", fail)
    with pytest.raises(RuntimeError, match="default browser") as error:
        _open_browser(session)
    assert session.token.get_secret_value() not in str(error.value)
