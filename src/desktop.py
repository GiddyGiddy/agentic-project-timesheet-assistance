import asyncio
import ctypes
import errno
import http.client
import json
import logging
import os
import secrets
import socket
import sys
import time
import webbrowser
from contextlib import contextmanager
from ctypes import wintypes
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from config.settings import default_user_data_dir
from config.user_settings import read_protected_file, write_protected_file
from src.web.app import HOST, PORT, create_app

SESSION_FILE = "desktop-session.dpapi"


class DesktopSession(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    port: int = Field(ge=1, le=65535, strict=True)
    token: SecretStr = Field(min_length=32, max_length=128)

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.port}/#token={self.token.get_secret_value()}"


@contextmanager
def _profile_lock(root: Path):
    if sys.platform != "win32":
        raise OSError("The desktop launcher requires Windows")
    import msvcrt

    runtime = root / "run"
    runtime.mkdir(parents=True, exist_ok=True)
    with (runtime / "desktop.lock").open("a+b") as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise
            yield False
        else:
            # Closing the descriptor releases the lock, including after a crash.
            yield True


@contextmanager
def _desktop_logging(root: Path):
    handler = RotatingFileHandler(
        root / "run" / "desktop.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    loggers = [logging.getLogger(name) for name in ("src", "uvicorn.error")]
    previous = [(logger.level, logger.propagate) for logger in loggers]
    try:
        for logger in loggers:
            logger.addHandler(handler)
            logger.setLevel(logging.WARNING)
            logger.propagate = False
        yield
    finally:
        for logger, (level, propagate) in zip(loggers, previous):
            logger.removeHandler(handler)
            logger.setLevel(level)
            logger.propagate = propagate
        handler.close()


def _open_browser(session: DesktopSession) -> None:
    try:
        opened = webbrowser.open(session.url)
    except (OSError, webbrowser.Error) as exc:
        raise RuntimeError("The default browser could not be opened. Check your Windows default browser and relaunch.") from exc
    if not opened:
        raise RuntimeError("The default browser could not be opened. Check your Windows default browser and relaunch.")


def _reopen_existing(root: Path, *, timeout: float = 30) -> None:
    path = root / "run" / SESSION_FILE
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not path.exists():
            time.sleep(0.1)
            continue
        try:
            session = DesktopSession.model_validate_json(read_protected_file(path))
        except FileNotFoundError:
            time.sleep(0.1)
            continue
        connection = http.client.HTTPConnection(HOST, session.port, timeout=1)
        try:
            connection.request(
                "GET", "/api/lifecycle",
                headers={"X-Timesheet-Token": session.token.get_secret_value()},
            )
            response = connection.getresponse()
            content = response.read(16385)
            if response.status != 200 or len(content) > 16384:
                raise RuntimeError("The running instance could not be verified. No browser was opened.")
            status = json.loads(content)
            if not isinstance(status, dict):
                raise RuntimeError("The running instance returned an invalid status. No browser was opened.")
            if status.get("exit_available") is not True:
                raise RuntimeError("The running service is not a desktop dashboard. No browser was opened.")
            if status.get("stopping") is not False:
                raise RuntimeError("Timesheet Assistant is shutting down. Wait for it to finish, then launch again.")
            _open_browser(session)
            return
        except (ConnectionError, TimeoutError, http.client.RemoteDisconnected):
            time.sleep(0.1)
        except http.client.HTTPException as exc:
            raise RuntimeError("The running instance returned an invalid HTTP response. No browser was opened.") from exc
        finally:
            connection.close()
    raise RuntimeError("The running instance did not become ready. Wait for startup or shutdown to finish and try again.")


async def _serve(listener: socket.socket, path: Path) -> None:
    session = DesktopSession(port=listener.getsockname()[1], token=SecretStr(secrets.token_urlsafe(32)))

    def request_shutdown() -> None:
        server.should_exit = True

    server = uvicorn.Server(uvicorn.Config(
        create_app(session.token.get_secret_value(), port=session.port, on_shutdown=request_shutdown),
        host=HOST, port=session.port, access_log=False, log_config=None,
    ))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        while not server.started:
            if serving.done():
                await serving
                raise RuntimeError("The dashboard could not start. Check the profile's run\\desktop.log.")
            await asyncio.sleep(0.05)
        write_protected_file(path, json.dumps({
            "port": session.port, "token": session.token.get_secret_value(),
        }).encode("utf-8"))
        await asyncio.to_thread(_open_browser, session)
        await serving
    finally:
        server.should_exit = True
        await serving


def run_desktop(*, data_dir: Path | None = None, port: int = PORT) -> None:
    root = Path(data_dir or os.environ.get("TIMESHEET_DATA_DIR") or default_user_data_dir()).expanduser()
    if not root.is_absolute():
        raise ValueError("TIMESHEET_DATA_DIR must be an absolute path")
    root = root.resolve()
    os.environ["TIMESHEET_DATA_DIR"] = str(root)
    with _profile_lock(root) as acquired:
        if not acquired:
            _reopen_existing(root)
            return
        path = root / "run" / SESSION_FILE
        path.unlink(missing_ok=True)
        with _desktop_logging(root), socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                listener.bind((HOST, port))
            except OSError as exc:
                raise RuntimeError(
                    "The local dashboard port is unavailable. Another application or development dashboard may be using it. It was not reused or stopped."
                ) from exc
            listener.listen(128)
            listener.setblocking(False)
            try:
                asyncio.run(_serve(listener, path))
            finally:
                path.unlink(missing_ok=True)


def _show_error(message: str) -> None:
    if sys.platform != "win32":
        print(message, file=sys.stderr)
        return
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
    user32.MessageBoxW.restype = ctypes.c_int
    user32.MessageBoxW(None, message, "Timesheet Assistant", 0x10)


def main() -> None:
    try:
        run_desktop()
    except (OSError, ValueError, RuntimeError) as exc:
        _show_error(str(exc))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
