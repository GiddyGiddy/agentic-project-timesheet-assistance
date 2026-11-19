import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from importlib.resources import files
from pathlib import Path

from config.settings import Settings
from src.graph.state import Registry


def _seed_profile(root: Path) -> None:
    for relative in (Path("config"), Path("data") / "templates", Path("output")):
        (root / relative).mkdir(parents=True, exist_ok=True)
    for name in ("projects_registry.json", "mcp_servers.json"):
        target = root / "config" / name
        content = files("config").joinpath(name).read_bytes()
        try:
            with target.open("xb") as stream:
                stream.write(content)
        except FileExistsError:
            continue


def initialize_user_storage(settings: Settings) -> None:
    if settings.timesheet_data_dir is not None:
        _seed_profile(settings.timesheet_data_dir)


def _source_file(root: Path, relative: Path) -> Path:
    source = root / relative
    if not source.resolve().is_relative_to(root):
        raise ValueError(f"Import source escapes the selected directory: {relative}")
    if not source.is_file():
        raise FileNotFoundError(f"Required import file is missing: {relative}")
    return source


def _backup_database(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
        with closing(sqlite3.connect(target)) as backup:
            original.backup(backup)
            if backup.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError(f"SQLite integrity check failed for {source.name}")


def import_user_data(source: Path, destination: Path) -> Path:
    source = source.expanduser().resolve()
    destination = destination.expanduser()
    if not destination.is_absolute():
        raise ValueError("Import destination must be an absolute path")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Import destination already exists; choose a new profile directory")
    destination = destination.resolve()
    if source.is_relative_to(destination) or destination.is_relative_to(source):
        raise ValueError("Import source and destination must be separate, non-nested directories")
    registry_path = Path("config") / "projects_registry.json"
    registry_bytes = _source_file(source, registry_path).read_bytes()
    Registry.model_validate_json(registry_bytes)
    databases = [Path("data") / name for name in ("timesheet.db", "checkpoints.db")]
    for relative in databases:
        _source_file(source, relative)
    optional_files = [Path("data") / "templates" / "timesheet_template.xlsx"]
    optional_files.extend(
        path.relative_to(source)
        for path in (source / "output").glob("timesheet-????-??.xlsx")
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".timesheet-import-", dir=destination.parent) as temporary:
        staging = Path(temporary) / "profile"
        _seed_profile(staging)
        (staging / registry_path).write_bytes(registry_bytes)
        for relative in databases:
            _backup_database(_source_file(source, relative), staging / relative)
        for relative in optional_files:
            if (source / relative).exists():
                shutil.copyfile(_source_file(source, relative), staging / relative)
        os.rename(staging, destination)
    return destination
