# Agentic Project Timesheet Assistant

The Agentic Project Timesheet Assistant is a local, single-user application that turns a day's Microsoft 365 activity into a proposed project timesheet. It collects Outlook email, calendar and Teams chat metadata through the Work IQ MCP server, asks an Azure OpenAI model to propose project hours, waits for explicit human approval, and exports approved hours to a copy of your Excel template. Nothing is saved as a timesheet before a person approves it.

It is built with Python, LangGraph, FastAPI, SQLAlchemy/SQLite, MSAL, `langchain-mcp-adapters` and `openpyxl`.

## Current status

| Area | Status |
|---|---|
| Azure OpenAI | Deployed and verified with synthetic activity (structured output). |
| Microsoft 365 sign-in | Verified: delegated browser sign-in with Microsoft Entra security defaults and MFA enabled. |
| Work IQ MCP connection | Verified: the server authenticates and lists its tools. |
| Work IQ data collection | Verified against a live Microsoft 365 test account: email, calendar and Teams chat collection produced a project-hour proposal that paused for human review. The test appointments were 60 and 45 minutes. |
| Mapping, approval, SQLite and Excel export | Verified end to end on synthetic data in an isolated sandbox (see [Sample run](#sample-run)). |
| Real project registry | Not yet supplied; `config\projects_registry.json` holds an example project. |
| Corporate Excel template | Not yet supplied; its coordinates are still to be configured. |
| Teams review message | Disabled. Approval happens in the dashboard or CLI; **Teams replies are not processed**. |
| Windows desktop readiness | Per-user setup, DPAPI-protected settings, browser launcher, single-instance reopening and explicit graceful exit are implemented. An unsigned standalone Windows x64 build passes isolated offline smoke tests. GitHub Actions build automation is configured; MSIX/signing/release publishing is still pending. |

## Solution Architecture

![Agentic Project Timesheet Assistant solution architecture using Azure icons](./docs/solution-architecture.svg)

### Resources and integrations

- **Local application:** a Python process on the operator's workstation. It runs the dashboard, CLI, scheduler, LangGraph workflow, collectors and exporters. No cloud hosting is used.
- **Microsoft Entra ID:** an app registration in the Microsoft 365 tenant. The app signs in the user with a delegated browser flow (authorization code with PKCE) using `http://localhost` as the redirect URI. Tokens are held in memory only.
- **Work IQ MCP server** (`https://workiq.svc.cloud.microsoft/mcp`): the Model Context Protocol endpoint the app calls. Its `fetch` tool maps to Microsoft Graph paths and needs the delegated permissions `WorkIQ.Read`, `WorkIQ.Act` and `WorkIQAgent.Ask`.
- **Work IQ:** the workplace-intelligence layer of Microsoft 365 Copilot behind that endpoint. It needs a Microsoft 365 Copilot licence for the user, Work IQ API usage-based billing and tenant policy approval.
- **Outlook email, Outlook calendar and Microsoft Teams:** the Microsoft 365 workloads read through Work IQ: inbox and Sent Items, the calendar view for the day, and one-to-one and group chat messages.
- **Azure OpenAI:** a Foundry resource with a `gpt-4.1-mini` deployment (Standard, pay-as-you-go). The app authenticates with an API key from development `.env`/environment variables or Windows-protected per-user settings. The Azure subscription does not need to be in the Microsoft 365 tenant.
- **Local data:** SQLite databases for approved entries and workflow checkpoints, a read-only Excel template, and generated workbooks in `output\`.

## Components

| Component | Module | Responsibility |
|---|---|---|
| Web dashboard | `src\web\app.py`, `src\web\static\` | Local-only FastAPI app: review, correct, approve or reject proposals; project registry and Excel layout; profile setup/template upload; monthly export and download; Microsoft 365 sign-in; in-process scheduler. |
| Protected configuration | `config\user_settings.py` | Validates service settings and atomically saves them using Windows user-scoped DPAPI. |
| Windows launcher | `src\desktop.py` | Per-profile process lock, protected runtime discovery, browser launch after readiness, rotating local diagnostics and graceful application exit. |
| CLI and scheduler | `src\main.py` | `daily`, `show`, `retry`, `approve`, `reject`, `export`, `schedule`, `workiq-check`. APScheduler runs the daily job at 18:00 and the month-end export at 18:30 on the last day. |
| Workflow | `src\graph\` | LangGraph graph: ingest, map, notify, review (interrupt), save. Shared state and system prompts. |
| Ingestion agent | `src\agents\ingestion_agent.py` | Calls the configured operations for emails, events and chats over the day window. |
| Mapper agent | `src\agents\mapper_agent.py` | Azure OpenAI with function-calling structured output; validates project codes and hours. |
| Review agent | `src\agents\review_agent.py` | Builds the review summary, optionally sends it, and pauses for approval. |
| Excel agent | `src\agents\excel_agent.py` | Aggregates approved monthly hours and triggers the Excel write. |
| MCP client | `src\integrations\mcp_client.py` | Reads `config\mcp_servers.json`, connects through `langchain-mcp-adapters`, caches tools, and routes the Work IQ adapter. |
| Work IQ collector | `src\integrations\workiq_collector.py` | Reads email, calendar and chat data with time windows, size limits and completeness checks. |
| Work IQ sign-in | `src\integrations\workiq_auth.py` | MSAL delegated browser sign-in; token in memory. |
| Excel writer | `src\integrations\excel_writer.py` | Copies the template, fills project rows by day column, preserves formulas, never overwrites. |
| Persistence | `src\database\` | SQLAlchemy models (`DailyEntry`, `ActivityAudit`) and repository. |

## Application capabilities

- **Daily proposal:** collects activity for one local day and proposes hours per registered project, with a summary and a confidence score for each entry.
- **Human approval:** every proposal pauses at a LangGraph interrupt. A person edits, approves or rejects it in the dashboard or CLI. The pause survives restarts through SQLite checkpoints.
- **Project registry:** `config\projects_registry.json` is the ground truth for project codes, clients, email domains, Teams channel names and keywords, and holds the Excel layout.
- **Monthly export:** approved entries are written to a new workbook copied from the template. Existing formulas are preserved and existing workbooks are never overwritten. Unfinished daily workflows block the export.
- **Scheduler:** optional in-process schedule for the daily proposal and the month-end export.

## Agentic AI

Deterministic code makes the decisions that must be exact. The language model only proposes entries, and every proposal is validated and then reviewed by a person.

![Agentic AI and approval workflow](./docs/agentic-workflow.svg)

### Mapper agent

| Aspect | Behaviour |
|---|---|
| Input | The day's activity records (email, meeting and chat metadata) and the project registry. |
| Output | Structured entries: `project_code`, `hours`, `summary`, `confidence`. |
| Estimation | Uses meeting durations where provided, deduplicates overlaps, estimates unscheduled work conservatively, and omits activity that cannot be mapped to a registered project. |
| Validation | Only registered project codes, hours in 0.25 steps, at most 24 hours per day. Invalid output fails the run. |
| Prompt safety | Email, calendar and chat content is treated as untrusted evidence, never as instructions. |
| Review | Entries below 0.80 confidence are flagged. Confidence is model-provided and is not a guarantee of accuracy. |

### Work IQ collection

Work IQ is configured in `config\mcp_servers.json` with `"adapter": "workiq_graph"` and reads through the `fetch` tool:

- **emails:** inbox (received) and Sent Items, drafts excluded. Only subject, sender and recipients, time and a 300-character preview are kept.
- **events:** the calendar view for the day. Cancelled and declined meetings are excluded and durations are clipped to the day.
- **chats:** Teams one-to-one and group chat messages created that day. **Teams channel messages are not collected.**

Work IQ caps results (100 items, 10 chat messages) and blocks paging. Full email/calendar windows are split into smaller time windows. Teams messages support only `createdDateTime lt` filtering, so the collector walks backwards from the day's end, overlaps timestamp boundaries and deduplicates messages. If a window cannot be split further, a full Teams window cannot advance past a timestamp, or too many chats were active to list, the run **fails** with an incomplete-collection error instead of proposing partial hours.

## Security and privacy

- **Local only:** the dashboard binds to `127.0.0.1:8765`, checks the host and origin, and requires a per-start private token. It is not an authentication system for other users and must not be exposed through a proxy or public network.
- **Secrets:** repository development uses plaintext `.env` or process environment variables; protect `.env` with OS permissions. Windows profiles can save service settings using user-scoped DPAPI. The profile setup form accepts a key but the API never returns it, even masked. Environment variables override saved settings; environment-controlled fields are read-only in setup. Nothing copies development credentials into a profile.
- **Protection boundary:** DPAPI normally requires the original Windows account and computer. It does not isolate the key from other programs running as that account, or encrypt SQLite/workbooks. No Microsoft 365 sign-in tokens or unused client secrets are saved.
- **Microsoft 365 tokens:** held in process memory and lost on restart. No client secret is used.
- **Data sent to the model:** activity previews are sent to your Azure OpenAI deployment. Limit source data and retention in line with your organization's policies. Avoid confidential detail in summaries.
- **Local retention:** approved entries, activity audit payloads and workflow checkpoints are stored in unencrypted SQLite files. Protect the working directory, backups and generated workbooks with OS access controls and encryption.
- **Least privilege:** the app only reads Microsoft 365 data. See [Known gaps](#known-gaps-and-follow-up-work) for the unused write permission it still requests.
- **Billing:** Work IQ API calls consume Copilot Credits and the Azure OpenAI deployment is billed per token. Use spending limits during testing.

## Configuration

For repository development, copy `.env.example` to `.env` and fill in the values. Relative paths are relative to the working directory, so run commands from the project root. Installed/per-user storage is described below.

| Variable | Purpose | Default |
|---|---|---|
| `AZURE_OPENAI_ENDPOINT` | Azure OpenAI resource root, for example `https://<resource>.openai.azure.com` | none |
| `AZURE_OPENAI_DEPLOYMENT_NAME` | Deployment name | none |
| `AZURE_OPENAI_API_KEY` | API key for the resource | none |
| `AZURE_OPENAI_API_VERSION` | API version | `2024-10-21` |
| `TENANT_ID` | Entra tenant that owns the Microsoft 365 data | none |
| `CLIENT_ID` | Application (client) ID of the app registration | none |
| `CLIENT_SECRET` | Not used by the Work IQ browser sign-in | none |
| `TIMEZONE` | IANA time zone for the day window and scheduler | `Europe/Berlin` |
| `TIMESHEET_DATA_DIR` | Absolute per-user data root, set in the process environment | unset in development; `%LOCALAPPDATA%\TimesheetAssistant` for frozen builds |
| `DATABASE_URL` | Approved entries and audit | `sqlite:///data/timesheet.db` |
| `CHECKPOINT_PATH` | LangGraph checkpoints | `data/checkpoints.db` |
| `MCP_CONFIG_PATH` | MCP servers and operations | `config/mcp_servers.json` |
| `PROJECTS_REGISTRY_PATH` | Projects and Excel layout | `config/projects_registry.json` |
| `TEMPLATE_PATH` | Read-only Excel template | `data/templates/timesheet_template.xlsx` |
| `OUTPUT_DIR` | Generated workbooks | `output` |

### Installation preparation: per-user storage

The MSIX-readiness work separates writable user data from application files, provides Windows-protected first-run setup, and includes a standalone Windows browser launcher with single-instance and shutdown handling. This is not yet an MSIX installer: signing and release publishing are subsequent steps.

- Repository runs keep their existing paths and `.env` behaviour unless opted into per-user storage.
- Frozen/packaged builds select `%LOCALAPPDATA%\TimesheetAssistant`. Set `TIMESHEET_DATA_DIR` to an absolute path to select another profile or test this mode from source.
- A profile contains `config\`, `data\` (SQLite databases, checkpoints and optional Excel template), and `output\`. First startup seeds the bundled project/MCP configuration only if missing. It never replaces existing configuration or creates a dummy Excel template.
- Per-user mode ignores the working-directory `.env`, including MCP substitutions. Enter service settings in the dashboard or supply process environment variables. No credentials or Microsoft 365 tokens are copied into a new profile.
- Explicit path overrides remain supported; relative overrides in per-user mode are rooted in the profile. Application resources continue to come from the installed package, not the working directory.
- Existing development data is never moved or imported automatically. User data is still unencrypted; back up the profile and exported workbooks. Do not rely on MSIX uninstall to preserve application data.

To try a fresh profile without modifying repository data, set this in the PowerShell session used to launch the app:

```powershell
$env:TIMESHEET_DATA_DIR = "$env:LOCALAPPDATA\TimesheetAssistant"
.\.venv\Scripts\python.exe -m src.web.app
```

#### First-run setup on Windows

Open the private dashboard URL printed at startup. An unconfigured profile opens **Setup & integrations** automatically.

1. Enter the Azure OpenAI HTTPS resource root, deployment, API version and key, your Entra tenant/client IDs, and an IANA time zone.
2. Select **Save service settings**. Values are saved atomically in `config\service-settings.dpapi` under the profile, encrypted with Windows user-scoped DPAPI, not plaintext JSON or `.env`. Saving makes no service calls and does not prove connectivity.
3. Replace the example project under **Projects** with your project codes.
4. Upload your existing macro-free `.xlsx` template and configure **Excel layout** to match. Upload accepts at most 10 MiB, 50 MiB expanded ZIP contents and 1,000 ZIP entries. It saves only to the configured template path and refuses to replace an existing/imported template. Upload validation does not establish that the project's export layout is correct.
5. Sign in to Microsoft 365, then generate and review a proposal. Approval and export remain separate explicit actions.

Leave the API key blank to retain the saved key; use **Remove the saved API key** to remove it. Only a configured/not-configured flag returns to the browser. Stop the dashboard scheduler before changing settings or uploading a template. Sign in again after changing tenant/client IDs; sign-in tokens still disappear on process restart.

Settings precedence is explicit constructor values, process environment variables, protected profile settings, then defaults. Repository-development mode is unchanged: it has no editable service settings or template-upload interface and still reads `.env`.

Protected saving/reading requires Windows. If the protected file becomes corrupt or cannot be decrypted, startup fails explicitly instead of falling back silently. First try the original Windows account/computer. To deliberately reconfigure, stop the app, back up the profile, rename only `config\service-settings.dpapi` out of the way, restart, and re-enter settings. Do not remove the databases, registry or template. Backups/imports on another account or computer may require re-entering credentials.

#### Explicit import of existing data

Close the source application and its scheduler first, and back up its data. Run import **before the first launch** of the destination profile. Import refuses an existing destination, including an empty directory; it does not merge or overwrite profiles.

```powershell
.\.venv\Scripts\python.exe -m src.main import-data `
  --source "C:\Repos\agentic-project-timesheet-assistant" `
  --destination "$env:LOCALAPPDATA\TimesheetAssistant" `
  --source-closed
```

The source must use the standard layout containing `data\timesheet.db`, `data\checkpoints.db`, and `config\projects_registry.json`. Custom source path overrides are not inferred from `.env`. The import validates the registry and snapshots both SQLite databases with SQLite's backup API, including committed WAL data. It preserves approved entries, activity audit and pending reviews. The destination is published only after the complete import succeeds.

Only the project registry, these databases, an optional `data\templates\timesheet_template.xlsx`, and `output\timesheet-????-??.xlsx` exports are imported. MCP configuration is reset to bundled defaults; source `.env`, credentials, sign-in caches and unrelated files are excluded. Sign in again and reconfigure service connections separately. Without `--destination`, import uses the process `TIMESHEET_DATA_DIR` or the default Windows per-user location.

For a different destination, set `TIMESHEET_DATA_DIR` to that same path when launching. To return to repository-development storage, remove the override from that shell:

```powershell
Remove-Item Env:\TIMESHEET_DATA_DIR
```

### Windows desktop launcher

After installing the project, start the windowless Windows GUI entry point:

```powershell
.\.venv\Scripts\timesheet-desktop.exe
```

The source equivalent is `.\.venv\Scripts\python.exe -m src.desktop`. Unlike the development dashboard, the desktop launcher always uses a per-user profile: `%LOCALAPPDATA%\TimesheetAssistant`, or the absolute `TIMESHEET_DATA_DIR` override. It never implicitly imports repository data or reads the repository `.env`.

- The launcher opens the default browser only after the loopback dashboard is ready.
- Starting it again for the same profile verifies the existing server using its private token and opens that dashboard instead of starting another process. Activation details in `run\desktop-session.dpapi` are encrypted for the Windows user; the token is not printed or logged.
- Closing or navigating away from a browser tab does **not** stop the process or its scheduler. Relaunch the desktop application to reopen the dashboard.
- Use **Exit application** in the navigation and confirm. New operations are blocked, the scheduler stops accepting work, and active operations finish before the server exits. Exit never approves pending hours or exports automatically. Unsaved browser edits are discarded.
- Microsoft 365 sign-in can take several minutes to complete or time out; an exit request waits for an active sign-in too. Do not force-kill the process unless accepting the risk of interrupting work.
- The OS releases the profile lock on a crash; the next launch replaces stale activation data. A leftover `run\desktop.lock` file is normal, not proof that the app is running.
- Another program, a different profile, or a manual development dashboard using `127.0.0.1:8765` produces a startup error. The launcher neither reuses nor stops that service. Do not run a separate CLI scheduler or manual dashboard against the same profile.
- Windowless startup errors show a Windows error dialog. Application/server warnings and errors go to rotating `run\desktop.log` files under the profile; access logging is disabled. Protect logs and the rest of the profile with OS permissions.

The existing `timesheet-ui` / `python -m src.web.app` command remains a manual development server: it prints a private URL, does not automatically open a browser, and retains repository storage unless explicitly opted into a profile. Its managed server also supports **Exit application**. Refreshing old browser pages is not a substitute for restarting an older server after code changes.

This launcher is not a Windows service, system-tray app, auto-start registration or finished MSIX package. Clean-machine installation and upgrade testing follow the packaging stage.

### Standalone Windows build (unsigned)

Build on Windows with x64 Python 3.11 or later. The initial build was verified using Python 3.14.2. After creating the virtual environment:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev,build]"
.\windows\build.ps1
```

The script runs PyInstaller, verifies required runtime/configuration/frontend/time-zone/CA resources and the x64 GUI executable, rejects forbidden user/development files, then writes:

- `dist\TimesheetAssistant\TimesheetAssistant.exe` and its `_internal\` runtime directory.
- `dist\TimesheetAssistant-<version>-win-x64-unsigned.zip`.
- A matching `.zip.sha256` checksum file.

The version comes from `pyproject.toml`. Build tools are pinned, but runtime dependencies use compatible ranges: this is **not** a byte-for-byte reproducible or fully locked release build. Generated build/distribution files are ignored by Git. A failed build must not be distributed; older archives from previous runs may remain.

Extract the **whole ZIP** to a writable location and run `TimesheetAssistant.exe`. Do not copy the executable alone or run it inside the ZIP: it needs the adjacent `_internal` directory. Python does not need to be installed for this bundle. The app opens the browser and uses the per-user profile described above; service configuration, Microsoft 365 entitlement/sign-in and an export template are still required. Stop a development dashboard before launching the bundle because both use port 8765.

This is a portable, **unsigned** test distribution, not an installer or production release. Windows may show trust/SmartScreen warnings or block it under organizational policy; follow your administrator's distribution policy rather than bypassing protection. Clean-machine testing, dependency/license review, MSIX identity/assets, trusted signing, upgrades and release publishing remain to be completed.

The opt-in smoke test launches the actual executable and opens browser tabs. It needs a free port 8765, strips Python from the child process's `PATH`, runs from a foreign working directory and uses only an isolated synthetic profile:

```powershell
# Close the running dashboard first. Prefer testing an extracted copy of the ZIP.
$env:TIMESHEET_FROZEN_EXE = (Resolve-Path .\dist\TimesheetAssistant\TimesheetAssistant.exe).Path
.\.venv\Scripts\python.exe -m pytest tests\test_frozen.py -q
Remove-Item Env:\TIMESHEET_FROZEN_EXE
```

Verified offline: frontend assets, profile seeding without reading a working-directory `.env`, DPAPI settings save/reload without returning keys, template upload/no-overwrite, real LangGraph/SQLite initialization, duplicate-instance reopening, explicit exit and restart persistence. The test never signs in, collects Microsoft 365 activity, calls the model, approves hours, exports or starts scheduled jobs. It does not certify external connectivity from the frozen build or a clean Windows machine. Normal regression runs skip this test unless explicitly enabled.

### GitHub Actions build artifacts

The [Windows standalone build workflow](.github/workflows/windows-build.yml) runs on pushes, pull requests and manual dispatch. Once committed and pushed, it uses a GitHub-hosted Windows 2025 x64 runner with Python 3.14.2 to:

1. Install the application, development and pinned build tooling, then check dependency compatibility.
2. Run the normal synthetic regression suite, including native Windows lifecycle/DPAPI tests.
3. Run the same `windows\build.ps1` used locally to build and verify the unsigned distribution.
4. Upload only the versioned ZIP and its SHA256 file after every preceding step succeeds.

No Azure, Microsoft 365 or signing secrets are needed. The workflow token has read-only repository access, checkout credentials are not persisted, and third-party action references are pinned to verified commit SHAs. Builds use compatible runtime dependency ranges, so the pinned interpreter/actions/build tools do not make releases fully reproducible.

To download a build, open the repository's **Actions > Windows standalone build**, select a successful run for the intended commit, and download `windows-x64-unsigned-<commit>-<attempt>` under **Artifacts**. Artifacts expire after seven days (subject to repository policy); access requires appropriate GitHub permissions, and Actions usage/storage is subject to the repository's plan and limits. Unpack GitHub's artifact download first, then extract the inner application ZIP in full. Compare `Get-FileHash -Algorithm SHA256 <application-zip>` with the adjacent `.sha256` file before running.

The browser-opening frozen smoke test remains opt-in/local and is skipped in CI. A successful CI run verifies the build and unit/native regression tests, **not** interactive packaged-app behavior or installation on a clean Windows machine. Test the downloaded artifact using the preceding smoke-test procedure before distribution. This workflow does not sign, create an MSIX, publish a GitHub Release, deploy to Azure or install anything for users. Manual dispatch becomes available after the workflow is on the default branch; GitHub may require approval for fork pull requests or repository Actions to be enabled.

## Setup

Use Python 3.11 or later. From the project root in PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

### Azure OpenAI

Create a deployment in an Azure OpenAI or Foundry resource and put its endpoint, deployment name and API key in development `.env` or the Windows profile setup form. Choose a model that supports function calling. Check the model's lifecycle status before relying on it; `gpt-4.1-mini` is currently marked as legacy.

### Microsoft 365 (Work IQ)

A tenant administrator must set up the following in the tenant that holds the mailbox:

1. Enable Work IQ for the tenant (the Work IQ service principal exists) and configure usage-based billing, including a spending policy that covers the **Work IQ API** and includes the user.
2. Assign a Microsoft 365 Copilot licence to the user.
3. Register a single-tenant app. Enable public client flows and add `http://localhost` as a **Mobile and desktop** redirect URI. Do not create a client secret.
4. Add the delegated Work IQ permissions `WorkIQ.Read`, `WorkIQ.Act` and `WorkIQAgent.Ask`, and grant admin consent. The MCP endpoint returns 403 without `WorkIQAgent.Ask`.
5. Review the Work IQ MCP tenant policy. Writes are blocked by default, which is why Teams sending is disabled.

Then set `TENANT_ID` and `CLIENT_ID` in `.env` and check the connection. This signs in through the browser and lists tools; it reads no mail or chat data:

```powershell
.\.venv\Scripts\python.exe -m src.main workiq-check
```

The dashboard has the same check under **Setup & integrations > Sign in to Microsoft 365**. Tokens live only in the current process. The dashboard scheduler uses the dashboard's session; for a CLI run, retry or scheduler pass `--workiq-sign-in` to sign in within that process. A scheduler cannot sign in unattended after a restart.

Tool discovery confirms connectivity, not permission to read every resource. Work IQ read failures include structured HTTP status/error codes when available, without logging source content. Teams collection includes messages at the day's start and excludes the next day's start. Failed collection remains resumable with `retry` and never generates a partial proposal.

### Projects

Edit `config\projects_registry.json` (or use the dashboard) and replace the example project with real project codes, clients, email domains, Teams channel names and keywords. Codes must be unique and match the project codes in the Excel template. `project_name` is optional extra context for the mapper and the review message.

### Excel template

Place your corporate `.xlsx` at `data\templates\timesheet_template.xlsx` or set `TEMPLATE_PATH`. Do not create a dummy corporate template. Configure `excel` in the registry:

- `sheet_name`: exact worksheet name.
- `header_row`: one-based row that contains each day number, unless explicit `day_columns` are given.
- `project_column`: one-based column that contains project codes.
- `first_project_row` and optional `last_project_row`: rows to inspect for project codes.
- Optional `month_header_cell`, `month_header_format`, `monthly_total_column` and `write_total_formula`.

By default each day of the exported month needs a unique header with its day number (integer, numeric text, or a date in that month). For a template without suitable headers, map days to columns explicitly:

```json
{
  "sheet_name": "Timesheet",
  "project_column": 1,
  "first_project_row": 6,
  "last_project_row": 30,
  "month_header_cell": "B2",
  "month_header_format": "%B %Y",
  "day_columns": {"1": "C", "2": "D", "3": "E", "...": "...", "31": "AG"},
  "monthly_total_column": "AH",
  "write_total_formula": false
}
```

Every day of the month must map to a distinct column, and the header, project, day and total locations must not overlap. If `write_total_formula` is true, a SUM formula is added to **blank** cells in the total column only; existing formulas are preserved and other values are never replaced. Each project with approved entries must appear exactly once, and its day cells must be ordinary writable cells (no formulas or merged cells). Registered project rows are cleared across the recognised day columns and then filled with approved hours; other rows are untouched.

The output is a new `.xlsx` and is never written over an existing file or the template. `openpyxl` does not calculate formulas, so open the result in Excel to recalculate. It may not preserve every advanced Excel feature; validate against a copy of your template first. Macro-enabled templates are not supported.

### Other MCP servers

The MCP client can also call any MCP server's tools directly. Define the server in `servers` and the operations in `operations` with the real tool names and arguments. The placeholders `{date}`, `{start_datetime}`, `{end_datetime}` and `{timezone}` are filled in, and `${NAME}` references read from the environment. The three read operations must return JSON records (an array of objects, or MCP text blocks holding JSON), and `send_review` must deliver `{message}` and report errors through MCP. Set `"disabled": true` on an operation to turn it off. Changing the configuration requires restarting a running scheduler.



## Known gaps and follow-up work

- **Production validation:** live Microsoft 365 collection and proposal generation have been verified with sample activity. High-volume histories, other tenant policies and representative production project mappings still need validation before relying on the application.
- **Teams:** channel messages are not collected, only one-to-one and group chats. The review message to Teams is disabled and Teams replies are not processed.
- **Unused write permission:** the sign-in requests `WorkIQ.Act` although the app only reads data. Remove it from the app registration and the requested scopes if you never enable Teams sending.
- **Unattended runs:** tokens are not persisted, so a restarted scheduler needs a person to sign in again.
- **Model lifecycle:** `gpt-4.1-mini` is marked as legacy by Azure and may need replacing.
- **Single user:** DPAPI protects saved profile settings, not access by other programs using the same account. There is no multi-user authentication or application-level user isolation; never share or publicly expose the dashboard.
- **Local storage:** SQLite files, workbooks and development `.env` are not encrypted by the application.
[![M8ven Score](https://m8ven.ai/badge/mcp/giddygiddy/agentic-project-timesheet-assistance)](https://m8ven.ai/mcp/giddygiddy/agentic-project-timesheet-assistance?s=readme)
