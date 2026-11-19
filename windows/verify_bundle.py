import argparse
from pathlib import Path


def verify_bundle(root: Path) -> None:
    if not root.is_dir():
        raise ValueError(f"Bundle directory does not exist: {root}")
    forbidden = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        name = path.name.lower()
        if (
            name.startswith(".env") or name.endswith((".dpapi", ".db", ".sqlite", ".sqlite3", ".xlsx", ".pfx", ".p12", ".key"))
            or any(suffix in name for suffix in (".db-", ".sqlite-", ".sqlite3-"))
            or name == "desktop.lock" or name.startswith("desktop.log")
            or any(part.lower() in {".git", ".venv", "sandbox", "tests"} for part in path.relative_to(root).parts)
        ):
            forbidden.append(str(path.relative_to(root)))
    if forbidden:
        raise ValueError("Bundle contains forbidden development/user files: " + ", ".join(sorted(forbidden)))
    internal = root / "_internal"
    required = [
        root / "TimesheetAssistant.exe",
        internal / "config" / "mcp_servers.json",
        internal / "config" / "projects_registry.json",
        internal / "certifi" / "cacert.pem",
        internal / "tzdata" / "zoneinfo" / "Europe" / "Berlin",
        *[internal / "src" / "web" / "static" / name for name in ("index.html", "app.js", "style.css")],
    ]
    missing = [str(path.relative_to(root)) for path in required if not path.is_file()]
    if not list(internal.glob("python3*.dll")):
        missing.append("embedded Python runtime DLL")
    if missing:
        raise ValueError("Bundle is incomplete: " + ", ".join(missing))

    import pefile

    with pefile.PE(str(root / "TimesheetAssistant.exe")) as executable:
        if executable.FILE_HEADER.Machine != 0x8664 or executable.OPTIONAL_HEADER.Subsystem != 2:
            raise ValueError("The executable must be a Windows x64 GUI application")


def main() -> None:
    parser = argparse.ArgumentParser(description="Check the standalone Windows bundle before distribution")
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    verify_bundle(args.bundle.resolve())
    print("Bundle verified: Windows x64 GUI, required resources present, no forbidden user/development files.")


if __name__ == "__main__":
    main()
