"""Generates docs/solution-architecture.svg and docs/agentic-workflow.svg.

Usage: python docs/generate-architecture-svg.py

Azure and Entra icons come from docs/icons/azure-icons.svg (Microsoft Azure Architecture
Icons). The other glyphs are simple symbols drawn below.
"""
import re
import sys
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
SPRITE = (HERE / "icons" / "azure-icons.svg").read_text(encoding="utf-8")
AZURE_SYMBOLS = re.findall(r"<symbol id=\"ic-[a-z]+\".*?</symbol>", SPRITE, re.S)

GLYPHS = {
    "db": '<ellipse cx="9" cy="4" rx="6" ry="2.4" fill="#0f766e"/><path d="M3 4v10c0 1.3 2.7 2.4 6 2.4s6-1.1 6-2.4V4c0 1.3-2.7 2.4-6 2.4S3 5.3 3 4z" fill="#2dd4bf"/><path d="M3 8.6c0 1.3 2.7 2.4 6 2.4s6-1.1 6-2.4" fill="none" stroke="#fff" stroke-width=".9"/>',
    "xls": '<rect x="2" y="1.5" width="14" height="15" rx="1.5" fill="#1d6f42"/><path d="M5.5 5.5l7 7M12.5 5.5l-7 7" stroke="#fff" stroke-width="1.7"/>',
    "doc": '<path d="M4 1.5h7l3.5 3.5v11.5H4z" fill="#1d6f42"/><path d="M11 1.5v3.5h3.5z" fill="#a7f3d0"/><path d="M6.2 9h5.6M6.2 11.5h5.6M6.2 14h3.4" stroke="#fff" stroke-width="1"/>',
    "web": '<rect x="1.5" y="2.5" width="15" height="10" rx="1.2" fill="#0f766e"/><rect x="3" y="4" width="12" height="7" fill="#d1fae5"/><path d="M6 15.5h6M9 12.5v3" stroke="#0f766e" stroke-width="1.4"/>',
    "cli": '<rect x="1.5" y="2.5" width="15" height="13" rx="1.5" fill="#1f2937"/><path d="M4.5 6.5l3 2.5-3 2.5" stroke="#34d399" stroke-width="1.5" fill="none"/><path d="M9.5 12h4" stroke="#fff" stroke-width="1.5"/>',
    "cfg": '<rect x="2" y="2" width="14" height="14" rx="1.5" fill="#6b7280"/><path d="M5 6h8M5 9h8M5 12h5" stroke="#fff" stroke-width="1.3"/>',
    "flow": '<circle cx="3.5" cy="9" r="2.3" fill="#0f766e"/><circle cx="14.5" cy="4" r="2.3" fill="#0f766e"/><circle cx="14.5" cy="14" r="2.3" fill="#0f766e"/><path d="M5.6 8.4L12.4 4.7M5.6 9.6L12.4 13.3" stroke="#0f766e" stroke-width="1.4" fill="none"/>',
    "plug": '<path d="M6 1.5v4M12 1.5v4" stroke="#0f766e" stroke-width="1.7"/><path d="M4 5.5h10v3.5a5 5 0 0 1-10 0z" fill="#0f766e"/><path d="M9 14v2.5" stroke="#0f766e" stroke-width="1.7"/>',
    "cloud": '<path d="M5 15a3.6 3.6 0 0 1-.5-7.2A4.6 4.6 0 0 1 13.3 7 3.9 3.9 0 0 1 13 15z" fill="#6b7280"/>',
    "check": '<circle cx="9" cy="9" r="7.5" fill="#6b7280"/><path d="M5.2 9.2l2.6 2.6 5-5.4" stroke="#fff" stroke-width="1.7" fill="none"/>',
    "stop": '<circle cx="9" cy="9" r="7.5" fill="#b45309"/><path d="M5.5 9h7" stroke="#fff" stroke-width="2"/>',
}
SYMBOLS = AZURE_SYMBOLS + [
    f'<symbol id="ic-{key}" viewBox="0 0 18 18">{body}</symbol>' for key, body in GLYPHS.items()
]

# Official Microsoft icons stored as separate files in docs/icons.
MICROSOFT_ICONS = {
    "workiq": "microsoft-365-copilot.svg",
    "mcp": "m365-toolbox.svg",
    "mail": "m365-mail.svg",
    "calendar": "m365-calendar.svg",
    "teams": "teams.svg",
}


def load_icon(key: str, filename: str) -> str:
    svg = (HERE / "icons" / filename).read_text(encoding="utf-8")
    svg = re.sub(r"<\?xml.*?\?>|<!DOCTYPE[^>]*>", "", svg).strip()
    viewbox = re.search(r'viewBox="([^"]+)"', svg).group(1)
    svg = re.sub(r'\bid="([^"]+)"', lambda m: f'id="{key}_{m.group(1)}"', svg)
    svg = re.sub(r"url\(#([^)]+)\)", lambda m: f"url(#{key}_{m.group(1)})", svg)
    svg = re.sub(r'(xlink:)?href="#([^"]+)"', lambda m: f'{m.group(1) or ""}href="#{key}_{m.group(2)}"', svg)
    inner = re.sub(r"^<svg[^>]*>|</svg>$", "", svg).strip()
    return f'<symbol id="ic-{key}" viewBox="{viewbox}">{inner}</symbol>'


SYMBOLS += [load_icon(key, name) for key, name in MICROSOFT_ICONS.items()]

COL = {"azure": "#0078d4", "svc": "#0f766e", "agent": "#7c3aed", "ext": "#6b7280"}
FILL = {"azure": "#eff6fc", "svc": "#ecfdf5", "agent": "#f5f0ff", "ext": "#f3f4f6"}
WARNINGS: list[str] = []


class Canvas:
    def __init__(self, title: str, width: int, height: int, tag_top: bool = False):
        self.title, self.w, self.h, self.tag_top = title, width, height, tag_top
        self.o: list[str] = []

    def a(self, text: str) -> None:
        self.o.append(text)

    def icon(self, key: str, x: int, y: int, size: int = 36) -> None:
        self.a(f'<use href="#ic-{key}" x="{x}" y="{y}" width="{size}" height="{size}"/>')

    def card(self, x, y, w, h, title, sub, kind, ic=None, dashed=False, tag=None):
        dash = ' stroke-dasharray="6 4"' if dashed else ""
        self.a(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{FILL[kind]}" stroke="{COL[kind]}" stroke-width="2"{dash}/>')
        tx = x + 14
        if ic:
            self.icon(ic, x + 12, y + 12, 36)
            tx = x + 58
        room = w - (tx - x) - 10
        if len(title) * 8.4 > room:
            WARNINGS.append(f"{self.title}: title too wide: {title!r}")
        self.a(f'<text x="{tx}" y="{y + 30}" class="t">{escape(title)}</text>')
        for i, line in enumerate(sub.split("\n")):
            if len(line) * 6.3 > room:
                WARNINGS.append(f"{self.title}: line too wide for {title!r}: {line!r}")
            self.a(f'<text x="{tx}" y="{y + 50 + i * 16}" class="s">{escape(line)}</text>')
        if tag:
            ty = y + (16 if self.tag_top else h - 8)
            self.a(f'<text x="{x + w - 10}" y="{ty}" class="tag" text-anchor="end" fill="{COL[kind]}">{escape(tag)}</text>')

    def arrow(self, pts, label=None, lx=None, ly=None, dashed=False, color="#374151", anchor="middle"):
        d = "M" + " L".join(f"{px},{py}" for px, py in pts)
        dash = ' stroke-dasharray="5 4"' if dashed else ""
        self.a(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.8"{dash} marker-end="url(#ah)"/>')
        if label:
            self.a(f'<text x="{lx}" y="{ly}" class="l" text-anchor="{anchor}">{escape(label)}</text>')

    def boundary(self, x, y, w, h, heading, hx, hy, anchor="start", icon=None):
        self.a(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" fill="#fafafa" stroke="#0078d4" stroke-width="2" stroke-dasharray="8 5"/>')
        if icon:
            self.icon(icon, x + 12, y + 8, 30)
        self.a(f'<text x="{hx}" y="{hy}" class="h" text-anchor="{anchor}">{escape(heading)}</text>')

    def legend(self, y, items, note):
        self.a(f'<text x="40" y="{y}" class="h">Legend</text>')
        lx = 110
        for kind, label, dashed in items:
            dash = ' stroke-dasharray="6 4"' if dashed else ""
            self.a(f'<rect x="{lx}" y="{y - 14}" width="26" height="18" rx="4" fill="{FILL[kind]}" stroke="{COL[kind]}" stroke-width="2"{dash}/>')
            self.a(f'<text x="{lx + 34}" y="{y}" class="s">{label}</text>')
            lx += 250
        self.a(f'<text x="40" y="{y + 32}" class="s">{escape(note)}</text>')

    def render(self) -> str:
        head = [
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.w} {self.h}" width="{self.w}" height="{self.h}" font-family="Segoe UI, Arial, sans-serif">',
            f"<title>{escape(self.title)}</title>",
            "<defs>" + "".join(SYMBOLS),
            '<marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#374151"/></marker>',
            '<style>.t{font-size:15px;font-weight:600;fill:#111827}.s{font-size:12px;fill:#4b5563}.l{font-size:12px;fill:#374151;paint-order:stroke;stroke:#fff;stroke-width:4px}.tag{font-size:10px;font-weight:600;text-transform:uppercase}.h{font-size:13px;font-weight:600;fill:#0078d4}.title{font-size:24px;font-weight:700;fill:#111827}</style>',
            "</defs>",
            f'<rect width="{self.w}" height="{self.h}" fill="#ffffff"/>',
            f'<text x="40" y="44" class="title">{escape(self.title)}</text>',
        ]
        return "\n".join(head + self.o + ["</svg>"])


LEGEND = [("azure", "Azure resource", False), ("svc", "Local component", False),
          ("agent", "AI agent", True), ("ext", "Microsoft 365 / external or human step", False)]


def solution_architecture() -> str:
    c = Canvas("Agentic Project Timesheet Assistant - Solution Architecture", 1720, 1060)
    a = c.a
    # operator
    a('<circle cx="90" cy="104" r="14" fill="#6b7280"/><path d="M62,152 Q62,126 90,126 Q118,126 118,152 Z" fill="#6b7280"/>')
    a('<text x="140" y="118" class="t">Operator</text><text x="140" y="136" class="s">Single trusted local user</text>')

    # local workstation
    c.boundary(40, 200, 1110, 600, "Local workstation - Python application, no cloud hosting", 1130, 229, "end", icon="web")
    c.card(70, 250, 330, 100, "Web dashboard", "FastAPI on 127.0.0.1:8765, local only\ntoken-protected review and export", "svc", "web", tag="local")
    c.card(70, 380, 330, 100, "CLI and scheduler", "timesheet CLI, APScheduler\ndaily 18:00, month-end 18:30", "svc", "cli", tag="local")
    c.card(70, 560, 330, 110, "Excel export", "excel_agent + openpyxl writer\ncopies the template, fills hours,\nnever overwrites a workbook", "svc", "xls", tag="local")

    c.card(480, 240, 290, 380, "LangGraph workflow", "shared state, prompts, graph\nhuman-in-the-loop interrupt", "svc", "flow", tag="orchestration")
    c.card(500, 330, 250, 56, "Ingestion agent", "collect emails, meetings, chats", "svc", tag="node")
    c.card(500, 396, 250, 56, "Mapper agent", "proposes project hours", "agent", "agent", dashed=True, tag="agent")
    c.card(500, 462, 250, 56, "Review agent", "summary, pause for approval", "svc", tag="node")
    c.card(500, 528, 250, 56, "Save approved hours", "validated entries to SQLite", "svc", tag="node")

    c.card(830, 250, 300, 120, "Work IQ integration", "windowed reads, completeness checks\nMSAL browser sign-in, in-memory token", "svc", "plug", tag="local")
    c.card(830, 480, 300, 90, "SQLAlchemy repository", "replace day, monthly totals\naudit payload kept per day", "svc", "db", tag="local")

    # Microsoft 365 tenant
    c.boundary(1230, 200, 460, 606, "Microsoft 365 tenant", 1250, 229)
    c.card(1250, 250, 420, 76, "Microsoft Entra ID", "app registration, delegated sign-in\nsecurity defaults and MFA apply", "azure", "entra")
    c.card(1250, 346, 420, 76, "Work IQ MCP server", "workiq.svc.cloud.microsoft/mcp\nfetch tool; WorkIQ.Read, Act, Agent.Ask", "ext", "mcp", tag="external")
    c.card(1250, 442, 420, 76, "Work IQ", "workplace intelligence of Copilot\nCopilot licence, billing, tenant policy", "ext", "workiq", tag="external")
    a('<rect x="1245" y="540" width="430" height="230" rx="10" fill="none" stroke="#6b7280" stroke-width="1.5" stroke-dasharray="4 4"/>')
    a('<text x="1260" y="558" class="s">Microsoft 365 workloads, read through Microsoft Graph</text>')
    c.card(1260, 566, 410, 62, "Outlook email", "inbox and Sent Items, previews only", "ext", "mail", tag="external")
    c.card(1260, 634, 410, 62, "Outlook calendar", "calendar view for the day", "ext", "calendar", tag="external")
    c.card(1260, 702, 410, 62, "Microsoft Teams", "one-to-one and group chat messages", "ext", "teams", tag="external")
    a('<text x="1250" y="792" class="s">Teams review message: disabled; writes need tenant policy.</text>')

    # Azure
    c.boundary(1230, 826, 460, 182, "Azure subscription", 1250, 855)
    c.card(1250, 870, 420, 110, "Azure OpenAI (Foundry resource)", "gpt-4.1-mini deployment, Standard\nkey kept in .env, never shown in the UI\ntemperature 0, structured output", "azure", "aoai")

    # data and files
    a('<text x="40" y="826" class="h">Local data and files</text>')
    c.card(40, 840, 200, 90, "Excel template", "read-only corporate\n.xlsx", "ext", "xls", tag="file")
    c.card(260, 840, 200, 90, "Output files", "timesheet-YYYY-MM\n.xlsx", "ext", "doc", tag="file")
    c.card(480, 840, 200, 90, "Checkpoints", "workflow state,\nresumable", "svc", "db", tag="sqlite")
    c.card(700, 840, 230, 90, "SQLite database", "timesheet.db:\nentries + audit", "svc", "db", tag="sqlite")
    c.card(950, 840, 230, 90, "Config and secrets", ".env, registry, MCP config\n(not committed)", "ext", "cfg", tag="files")

    # flows
    c.arrow([(160, 156), (160, 250)], "browser", 172, 232, anchor="start")
    c.arrow([(70, 156), (55, 156), (55, 430), (70, 430)])
    c.arrow([(400, 300), (480, 300)], "run, approve", 440, 292)
    c.arrow([(400, 430), (480, 430)], "daily, retry", 440, 422)
    c.arrow([(235, 480), (235, 560)], "month-end", 245, 525, anchor="start")
    c.arrow([(750, 358), (830, 358)], "collect", 790, 350)
    c.arrow([(750, 556), (830, 556)], "approved", 790, 548)
    c.arrow([(1130, 275), (1250, 275)], "MSAL sign-in", 1190, 267)
    c.arrow([(1130, 335), (1210, 335), (1210, 384), (1250, 384)], "HTTPS MCP", 1170, 328)
    c.arrow([(1460, 422), (1460, 442)])
    c.arrow([(1460, 518), (1460, 540)], "Microsoft Graph", 1470, 534, anchor="start")
    c.arrow([(750, 424), (1190, 424), (1190, 925), (1250, 925)], "structured output, API key", 960, 442)
    c.arrow([(900, 570), (900, 840)], "upsert day", 910, 640, anchor="start")
    c.arrow([(580, 620), (580, 840)], "pause / resume", 590, 790, anchor="start")
    c.arrow([(830, 840), (830, 720), (440, 720), (440, 630), (400, 630)], "monthly totals", 740, 712)
    c.arrow([(200, 840), (200, 670)], "read-only", 210, 790, anchor="start")
    c.arrow([(385, 670), (385, 840)], "new .xlsx", 395, 790, anchor="start")

    c.legend(1000, LEGEND, "Azure and Entra icons: Microsoft Azure Architecture Icons. Mail, calendar and tools: Microsoft 365 architecture icons. Teams and Microsoft 365 Copilot (used for Work IQ): Microsoft product icons.")
    return c.render()


def agentic_workflow() -> str:
    c = Canvas("Agentic Project Timesheet Assistant - Agentic AI and Approval Workflow", 1720, 880, tag_top=True)
    a = c.a
    a('<text x="40" y="84" class="h">1. Daily proposal: collect, propose, validate, then a human approves (LangGraph)</text>')
    c.card(40, 110, 230, 84, "Trigger", "dashboard, CLI or scheduler\n18:00 local time", "svc", "web", tag="local")
    c.card(320, 110, 230, 84, "Ingest", "Work IQ: emails, meetings,\nTeams chats, windowed", "svc", "workiq", tag="node")
    c.card(600, 110, 230, 84, "Mapper agent", "registry + activity in prompt\nstructured output", "agent", "agent", dashed=True, tag="agent")
    c.card(880, 110, 230, 84, "Validators", "known project codes,\n0.25 h steps, max 24 h", "svc", "flow", tag="code")
    c.card(1160, 110, 230, 84, "Notify", "send_review: disabled\nTeams summary when on", "ext", "teams", dashed=True, tag="optional")
    c.card(1440, 110, 230, 84, "Review gate", "interrupt: human edits,\napproves or rejects", "ext", "check", tag="human")

    c.card(320, 260, 230, 84, "Incomplete data", "result cap reached or too\nmany chats: run fails", "ext", "stop", tag="stops")
    c.card(600, 260, 230, 84, "Azure OpenAI", "gpt-4.1-mini, temperature 0\nkey in .env", "azure", "aoai")
    c.card(880, 260, 230, 84, "Project registry", "ground truth: codes, domains,\nchannels, keywords", "svc", "cfg", tag="config")
    c.card(1160, 260, 230, 84, "Retry and resume", "retry fixes a failed step;\nreview pause survives restart", "svc", "db", tag="sqlite")
    c.card(1440, 260, 230, 84, "Save", "approved entries replace the\nday, audit payload kept", "svc", "db", tag="sqlite")

    c.arrow([(270, 152), (320, 152)])
    c.arrow([(550, 152), (600, 152)], "data", 575, 142)
    c.arrow([(830, 152), (880, 152)], "entries", 855, 142)
    c.arrow([(1110, 152), (1160, 152)], "valid", 1135, 142)
    c.arrow([(1390, 152), (1440, 152)])
    c.arrow([(435, 194), (435, 260)], "cap hit", 445, 232, anchor="start", dashed=True)
    c.arrow([(715, 194), (715, 260)], "chat", 725, 232, anchor="start")
    c.arrow([(995, 260), (995, 194)], "valid codes", 1005, 232, anchor="start")
    c.arrow([(1275, 194), (1275, 260)], "failed step", 1285, 232, anchor="start", dashed=True)
    c.arrow([(1555, 194), (1555, 260)], "approved", 1565, 232, anchor="start")

    a('<line x1="40" y1="388" x2="1680" y2="388" stroke="#d1d5db" stroke-dasharray="4 4"/>')
    a('<text x="40" y="424" class="h">2. Month-end export: approved data only, written to a copy of the template</text>')
    c.card(40, 450, 230, 84, "Trigger", "export --month, dashboard\nor 18:30 on the last day", "svc", "cli", tag="local")
    c.card(320, 450, 230, 84, "Pending check", "blocked while any daily\nworkflow is unfinished", "svc", "flow", tag="code")
    c.card(600, 450, 230, 84, "Aggregate", "monthly totals per\nproject and day", "svc", "db", tag="sqlite")
    c.card(880, 450, 230, 84, "Excel writer", "openpyxl: project rows x\nday columns, formulas kept", "svc", "xls", tag="local")
    c.card(1160, 450, 230, 84, "Output workbook", "timesheet-YYYY-MM.xlsx,\nnever overwritten", "ext", "doc", tag="file")
    c.card(1440, 450, 230, 84, "Human check", "recalculate in Excel;\nverify completeness", "ext", "check", tag="human")
    c.card(320, 600, 230, 84, "Blocked", "approve or reject the\nunfinished days first", "ext", "stop", tag="stops")
    c.card(880, 600, 230, 84, "Excel template", "read-only; layout set in\nthe project registry", "ext", "xls", tag="file")

    c.arrow([(270, 492), (320, 492)])
    c.arrow([(550, 492), (600, 492)], "clear", 575, 482)
    c.arrow([(830, 492), (880, 492)], "totals", 855, 482)
    c.arrow([(1110, 492), (1160, 492)], "copy", 1135, 482)
    c.arrow([(1390, 492), (1440, 492)])
    c.arrow([(435, 534), (435, 600)], "pending", 445, 572, anchor="start", dashed=True)
    c.arrow([(995, 600), (995, 534)], "read only", 1005, 572, anchor="start")

    a('<text x="40" y="730" class="h">Guardrails</text>')
    for i, line in enumerate([
        "Agent output is a proposal: nothing is saved until a human approves it in the dashboard or CLI; rejecting saves nothing.",
        "The mapper may use only registered project codes; hours are 0.25 h steps and at most 24 h per day.",
        "Email, calendar and chat content is untrusted evidence, never instructions; previews are limited to 300 characters.",
        "Incomplete collection fails the run instead of proposing partial hours. Teams channel messages are not collected.",
        "Exports include only approved data and never overwrite an existing workbook or the template.",
    ]):
        a(f'<text x="40" y="{756 + i * 20}" class="s">- {escape(line)}</text>')
    a('<text x="40" y="866" class="s">Icons: Microsoft Azure Architecture Icons; Teams and Microsoft 365 Copilot (Work IQ): Microsoft product icons. Dashed purple = AI agent; green = local component; blue = Azure resource; grey = Microsoft 365, external or human step.</text>')
    return c.render()


if __name__ == "__main__":
    for name, builder in (("solution-architecture.svg", solution_architecture), ("agentic-workflow.svg", agentic_workflow)):
        (HERE / name).write_text(builder(), encoding="utf-8")
        print("written", HERE / name)
    for warning in WARNINGS:
        print("WARNING:", warning)
    sys.exit(0)
