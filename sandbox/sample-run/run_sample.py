"""End-to-end run on synthetic data in an isolated sandbox. Never touches real config or data."""
import asyncio
import json
import os
import sys
from datetime import date
from pathlib import Path

from openpyxl import Workbook, load_workbook

SANDBOX = Path(__file__).parent.resolve()
DAY = date(2026, 11, 13)

for name in ("timesheet.db", "checkpoints.db", "review_message.txt"):
    (SANDBOX / name).unlink(missing_ok=True)
for old in (SANDBOX / "output").glob("*.xlsx"):
    old.unlink()

os.environ.update({
    "DATABASE_URL": f"sqlite:///{(SANDBOX / 'timesheet.db').as_posix()}",
    "CHECKPOINT_PATH": str(SANDBOX / "checkpoints.db"),
    "PROJECTS_REGISTRY_PATH": str(SANDBOX / "projects_registry.json"),
    "MCP_CONFIG_PATH": str(SANDBOX / "mcp_servers.json"),
    "TEMPLATE_PATH": str(SANDBOX / "timesheet_template.xlsx"),
    "OUTPUT_DIR": str(SANDBOX / "output"),
})

(SANDBOX / "mcp_servers.json").write_text(json.dumps({
    "servers": {"sample": {
        "transport": "stdio", "command": sys.executable, "args": [str(SANDBOX / "sample_server.py")],
    }},
    "operations": {
        "emails": {"server": "sample", "tool": "emails", "arguments": {"day": "{date}"}},
        "events": {"server": "sample", "tool": "events", "arguments": {"day": "{date}"}},
        "chats": {"server": "sample", "tool": "chats", "arguments": {"day": "{date}"}},
        "send_review": {"server": "sample", "tool": "send_review", "arguments": {"message": "{message}"}},
    },
}, indent=2), encoding="utf-8")

workbook = Workbook()
sheet = workbook.active
sheet.title = "Timesheet"
sheet["A1"] = "Project"
for day in range(1, 32):
    sheet.cell(1, day + 1, day)
sheet["AG1"] = "Total"
for row, code in enumerate(("ALPHA-001", "BETA-002", "GAMMA-003"), start=2):
    sheet.cell(row, 1, code)
    sheet.cell(row, 33, f"=SUM(B{row}:AF{row})")
sheet["A6"] = "Month"
workbook.save(SANDBOX / "timesheet_template.xlsx")

from config.settings import Settings  # noqa: E402  (after the environment is isolated)
from src.main import checked_export, graph_action, load_registry  # noqa: E402

settings = Settings()
assert settings.database_url.startswith(f"sqlite:///{SANDBOX.as_posix()}"), "Sandbox isolation failed"
registry = load_registry(settings.projects_registry_path)


async def main():
    proposal = await graph_action(settings, registry, DAY, "daily", verbose=False)
    print("Proposal (not yet approved), pending:", proposal["pending_nodes"])
    for entry in proposal["entries"]:
        print(f"  {entry['project_code']}: {entry['hours']}h, confidence {entry['confidence']} - {entry['summary']}")
    print("Total proposed:", sum(e["hours"] for e in proposal["entries"]))
    review = SANDBOX / "review_message.txt"
    print("Review message sent through MCP:", review.is_file())
    approved = await graph_action(settings, registry, DAY, "approve", verbose=False)
    print("Approval status:", approved["approval_status"])
    output = await checked_export(settings, registry, DAY.year, DAY.month)
    print("Exported:", output.name)
    sheet = load_workbook(output)["Timesheet"]
    for row in range(2, 5):
        print("  ", sheet.cell(row, 1).value, "| day 13 =", sheet.cell(row, 14).value, "| total formula:", sheet.cell(row, 33).value)
    print("  Month header:", sheet["B6"].value)


asyncio.run(main())
