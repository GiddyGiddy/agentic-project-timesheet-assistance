from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH).parent
datas = [
    (str(root / "config" / name), "config")
    for name in ("mcp_servers.json", "projects_registry.json")
]
datas += [
    (str(root / "src" / "web" / "static" / name), str(Path("src") / "web" / "static"))
    for name in ("index.html", "app.js", "style.css")
]
datas += collect_data_files("tzdata")
datas += copy_metadata("agentic-project-timesheet-assistant", recursive=True)
hiddenimports = []
for package in ("langchain_core", "langchain_openai", "langgraph", "tiktoken_ext", "uvicorn"):
    hiddenimports += collect_submodules(package)

a = Analysis(
    [str(root / "windows" / "desktop_entry.py")],
    pathex=[str(root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "_pytest", "IPython", "ipykernel", "notebook"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='TimesheetAssistant',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='TimesheetAssistant',
)
